import type {
  ClipDTO,
  MaterialDTO,
  ProjectDTO,
  RationalTimeDTO,
  TrackDTO,
} from "@aether/contracts";

export const OPENREEL_SCHEMA_VERSION = "1.0.0";
export const OPENREEL_VERSION = "0.1.1";
export const OPENREEL_COMMIT = "8459024d4c82ee16a2e14537553884a623ae9c4e";

function seconds(time: RationalTimeDTO): number {
  return time.value / time.timescale;
}

function dateMillis(value: string): number {
  const parsed = Date.parse(value);
  if (!Number.isFinite(parsed)) {
    throw new Error(`Invalid project date: ${value}`);
  }
  return parsed;
}

const defaultTransform = {
  position: { x: 0, y: 0 },
  scale: { x: 1, y: 1 },
  rotation: 0,
  anchor: { x: 0.5, y: 0.5 },
  opacity: 1,
  fitMode: "contain" as const,
};

interface OpenReelClip {
  id: string;
  mediaId: string;
  trackId: string;
  startTime: number;
  duration: number;
  inPoint: number;
  outPoint: number;
  effects: [];
  audioEffects: [];
  transform: typeof defaultTransform;
  volume: number;
  keyframes: [];
}

interface OpenReelTrack {
  id: string;
  type: "video" | "audio";
  name: string;
  clips: OpenReelClip[];
  transitions: [];
  locked: false;
  hidden: false;
  muted: false;
  solo: false;
}

export interface OpenReelProjectFile {
  version: "1.0.0";
  project: {
    id: string;
    name: string;
    createdAt: number;
    modifiedAt: number;
    settings: {
      width: 1920;
      height: 1080;
      frameRate: 24;
      sampleRate: 48000;
      channels: 2;
    };
    mediaLibrary: {
      items: Array<{
        id: string;
        name: string;
        type: MaterialDTO["type"];
        fileHandle: null;
        blob: null;
        metadata: {
          duration: number;
          width: number;
          height: number;
          frameRate: number;
          codec: "unknown";
          sampleRate: 48000;
          channels: 2;
          fileSize: 0;
        };
        thumbnailUrl: string;
        waveformData: null;
        isPlaceholder: true;
        originalUrl: string;
      }>;
    };
    timeline: {
      tracks: OpenReelTrack[];
      subtitles: [];
      duration: number;
      markers: [];
      backgroundFillMode: "color";
      layoutBackgroundColor: "#000000";
    };
  };
  metadata: {
    exportedAt: number;
    description: string;
  };
}

function convertClip(clip: ClipDTO): OpenReelClip {
  const inPoint = seconds(clip.sourceIn);
  const duration = seconds(clip.duration);
  return {
    id: clip.id,
    mediaId: clip.materialId,
    trackId: clip.trackId,
    startTime: seconds(clip.start),
    duration,
    inPoint,
    outPoint: inPoint + duration,
    effects: [],
    audioEffects: [],
    transform: { ...defaultTransform },
    volume: 1,
    keyframes: [],
  };
}

function convertTrack(track: TrackDTO): OpenReelTrack | null {
  if (track.type === "subtitle") return null;
  return {
    id: track.id,
    type: track.type,
    name: track.name,
    clips: track.clips.map(convertClip),
    transitions: [],
    locked: false,
    hidden: false,
    muted: false,
    solo: false,
  };
}

export function createOpenReelProjectFile(project: ProjectDTO): OpenReelProjectFile {
  const tracks = project.timeline.tracks.flatMap((track) => {
    const converted = convertTrack(track);
    return converted ? [converted] : [];
  });
  const duration = Math.max(
    0,
    ...tracks.flatMap((track) =>
      track.clips.map((clip) => clip.startTime + clip.duration)),
  );
  const subtitleCount = project.timeline.tracks
    .filter((track) => track.type === "subtitle")
    .reduce((count, track) => count + track.clips.length, 0);
  const modifiedAt = dateMillis(project.updatedAt);

  return {
    version: OPENREEL_SCHEMA_VERSION,
    project: {
      id: project.id,
      name: project.name,
      createdAt: dateMillis(project.createdAt),
      modifiedAt,
      settings: {
        width: 1920,
        height: 1080,
        frameRate: 24,
        sampleRate: 48_000,
        channels: 2,
      },
      mediaLibrary: {
        items: project.materials.map((material) => ({
          id: material.id,
          name: material.name,
          type: material.type,
          fileHandle: null,
          blob: null,
          metadata: {
            duration: material.duration ? seconds(material.duration) : 0,
            width: material.type === "audio" ? 0 : 1920,
            height: material.type === "audio" ? 0 : 1080,
            frameRate: material.type === "video" ? 24 : 0,
            codec: "unknown",
            sampleRate: 48_000,
            channels: 2,
            fileSize: 0,
          },
          thumbnailUrl: material.url,
          waveformData: null,
          isPlaceholder: true,
          originalUrl: material.url,
        })),
      },
      timeline: {
        tracks,
        subtitles: [],
        duration,
        markers: [],
        backgroundFillMode: "color",
        layoutBackgroundColor: "#000000",
      },
    },
    metadata: {
      exportedAt: modifiedAt,
      description: subtitleCount > 0
        ? `Exported by Aether Studio; ${subtitleCount} subtitle clip(s) require manual migration.`
        : "Exported by Aether Studio for OpenReel schema 1.0.0.",
    },
  };
}
