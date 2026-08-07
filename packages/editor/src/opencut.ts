import type {
  MaterialDTO,
  ProjectDTO,
  RationalTimeDTO,
  TrackDTO,
} from "@aether/contracts";
import {
  TICKS_PER_SECOND,
  formatTimecode,
  mediaTimeFromSeconds,
  mediaTimeToSeconds,
  roundToFrame,
  type FrameRate,
} from "opencut-wasm";

export const OPENCUT_WASM_VERSION = "0.2.10";
export const OPENCUT_CLASSIC_COMMIT =
  "cf5e79e919144200294fb9fed22a222592a0aeea";

const DEFAULT_FRAME_RATE: FrameRate = { numerator: 24, denominator: 1 };
const OPENCUT_CLASSIC_PROJECT_VERSION = 31;

function seconds(time: RationalTimeDTO): number {
  return time.value / time.timescale;
}

function mediaTime(time: RationalTimeDTO): number {
  const value = mediaTimeFromSeconds({ seconds: seconds(time) });
  if (value === undefined) {
    throw new Error("OpenCut rejected the supplied media time");
  }
  return value;
}

export function getOpenCutTicksPerSecond(): number {
  return TICKS_PER_SECOND();
}

export function toOpenCutMediaTime(time: RationalTimeDTO): number {
  return mediaTime(time);
}

export function fromOpenCutMediaTime(time: number): RationalTimeDTO {
  return {
    value: Math.round(time),
    timescale: getOpenCutTicksPerSecond(),
  };
}

export function snapToOpenCutFrame(
  time: RationalTimeDTO,
  rate: FrameRate = DEFAULT_FRAME_RATE,
): RationalTimeDTO {
  const snapped = roundToFrame({ time: mediaTime(time), rate });
  if (snapped === undefined) {
    throw new Error("OpenCut could not align the supplied media time to a frame");
  }
  return fromOpenCutMediaTime(snapped);
}

export function formatOpenCutTimecode(
  time: RationalTimeDTO,
  rate: FrameRate = DEFAULT_FRAME_RATE,
): string {
  const label = formatTimecode({
    time: mediaTime(time),
    format: "HH:MM:SS:FF",
    rate,
  });
  if (label === undefined) {
    throw new Error("OpenCut could not format the supplied media time");
  }
  return label;
}

interface OpenCutElement {
  id: string;
  type: "video" | "image" | "audio";
  name: string;
  mediaId: string;
  startTime: number;
  duration: number;
  trimStart: number;
  trimEnd: number;
  sourceDuration?: number;
  sourceType?: "upload";
  isSourceAudioEnabled?: boolean;
  hidden?: boolean;
  params: Record<string, number | string>;
}

interface OpenCutTrack {
  id: string;
  name: string;
  type: "video" | "audio";
  elements: OpenCutElement[];
  muted: boolean;
  hidden?: boolean;
}

export interface OpenCutCompatibilitySnapshot {
  format: "aether-opencut-compat/v1";
  generatedBy: {
    application: "Aether Studio";
    opencutWasmVersion: string;
    opencutClassicCommit: string;
  };
  project: {
    metadata: {
      id: string;
      name: string;
      duration: number;
      createdAt: string;
      updatedAt: string;
    };
    scenes: Array<{
      id: string;
      name: string;
      isMain: true;
      tracks: {
        main: OpenCutTrack;
        overlay: OpenCutTrack[];
        audio: OpenCutTrack[];
      };
      bookmarks: [];
      createdAt: string;
      updatedAt: string;
    }>;
    currentSceneId: string;
    settings: {
      fps: FrameRate;
      canvasSize: { width: 1920; height: 1080 };
      canvasSizeMode: "preset";
      lastCustomCanvasSize: null;
      originalCanvasSize: null;
      background: { type: "color"; color: "#000000" };
    };
    version: number;
    timelineViewState: {
      zoomLevel: 1;
      scrollLeft: 0;
      playheadTime: 0;
    };
  };
  mediaManifest: Array<{
    id: string;
    name: string;
    type: MaterialDTO["type"];
    sourceUrl: string;
    duration?: number;
  }>;
  warnings: string[];
}

const visualParams = {
  "transform.positionX": 0,
  "transform.positionY": 0,
  "transform.scaleX": 1,
  "transform.scaleY": 1,
  "transform.rotate": 0,
  opacity: 1,
  blendMode: "normal",
};

function convertTrack(
  track: TrackDTO,
  materials: Map<string, MaterialDTO>,
  warnings: string[],
): OpenCutTrack {
  const elements = track.clips.flatMap((clip): OpenCutElement[] => {
    const material = materials.get(clip.materialId);
    if (!material) {
      warnings.push(`Clip ${clip.id} references missing material ${clip.materialId}`);
      return [];
    }

    const duration = mediaTime(clip.duration);
    const trimStart = mediaTime(clip.sourceIn);
    const sourceDuration = material.duration
      ? mediaTime(material.duration)
      : trimStart + duration;
    const trimEnd = Math.max(0, sourceDuration - trimStart - duration);

    if (trimStart + duration > sourceDuration) {
      warnings.push(`Clip ${clip.id} extends beyond material ${material.id}`);
    }

    if (track.type === "audio") {
      return [{
        id: clip.id,
        type: "audio",
        sourceType: "upload",
        name: material.name,
        mediaId: material.id,
        startTime: mediaTime(clip.start),
        duration,
        trimStart,
        trimEnd,
        sourceDuration,
        params: { volume: 0 },
      }];
    }

    return [{
      id: clip.id,
      type: material.type === "image" ? "image" : "video",
      name: material.name,
      mediaId: material.id,
      startTime: mediaTime(clip.start),
      duration,
      trimStart,
      trimEnd,
      sourceDuration,
      isSourceAudioEnabled: material.type === "video" ? true : undefined,
      hidden: false,
      params: { ...visualParams },
    }];
  });

  return {
    id: track.id,
    name: track.name,
    type: track.type === "audio" ? "audio" : "video",
    elements,
    muted: false,
    hidden: track.type === "audio" ? undefined : false,
  };
}

export function createOpenCutCompatibilitySnapshot(
  project: ProjectDTO,
): OpenCutCompatibilitySnapshot {
  const materials = new Map(project.materials.map((item) => [item.id, item]));
  const warnings: string[] = [];
  const videoTracks = project.timeline.tracks.filter((track) => track.type === "video");
  const audioTracks = project.timeline.tracks.filter((track) => track.type === "audio");
  const subtitleTracks = project.timeline.tracks.filter((track) => track.type === "subtitle");

  if (subtitleTracks.some((track) => track.clips.length > 0)) {
    warnings.push("OpenCut compatibility snapshots do not yet translate subtitle tracks");
  }

  const emptyMain: OpenCutTrack = {
    id: "aether-main-video",
    name: "Main Video",
    type: "video",
    elements: [],
    muted: false,
    hidden: false,
  };
  const convertedVideo = videoTracks.map((track) =>
    convertTrack(track, materials, warnings));
  const sceneId = `${project.id}-main-scene`;
  const duration = Math.max(
    0,
    ...project.timeline.tracks.flatMap((track) =>
      track.clips.map((clip) =>
        mediaTimeToSeconds({
          time: mediaTime(clip.start) + mediaTime(clip.duration),
        }))),
  );

  return {
    format: "aether-opencut-compat/v1",
    generatedBy: {
      application: "Aether Studio",
      opencutWasmVersion: OPENCUT_WASM_VERSION,
      opencutClassicCommit: OPENCUT_CLASSIC_COMMIT,
    },
    project: {
      metadata: {
        id: project.id,
        name: project.name,
        duration: Math.round(duration * getOpenCutTicksPerSecond()),
        createdAt: project.createdAt,
        updatedAt: project.updatedAt,
      },
      scenes: [{
        id: sceneId,
        name: "Main scene",
        isMain: true,
        tracks: {
          main: convertedVideo[0] ?? emptyMain,
          overlay: convertedVideo.slice(1),
          audio: audioTracks.map((track) => convertTrack(track, materials, warnings)),
        },
        bookmarks: [],
        createdAt: project.createdAt,
        updatedAt: project.updatedAt,
      }],
      currentSceneId: sceneId,
      settings: {
        fps: DEFAULT_FRAME_RATE,
        canvasSize: { width: 1920, height: 1080 },
        canvasSizeMode: "preset",
        lastCustomCanvasSize: null,
        originalCanvasSize: null,
        background: { type: "color", color: "#000000" },
      },
      version: OPENCUT_CLASSIC_PROJECT_VERSION,
      timelineViewState: { zoomLevel: 1, scrollLeft: 0, playheadTime: 0 },
    },
    mediaManifest: project.materials.map((material) => ({
      id: material.id,
      name: material.name,
      type: material.type,
      sourceUrl: material.url,
      duration: material.duration ? mediaTime(material.duration) : undefined,
    })),
    warnings,
  };
}
