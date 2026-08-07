import { RationalTime, MaterialDTO, ClipDTO, TimelineDTO } from "@aether/contracts";

export * from "./opencut";
export * from "./openreel";

/**
 * IMaterialLoader defines the abstract layer for loading materials
 * and resolving their metadata, independent of any underlying player/editor engine.
 */
export interface IMaterialLoader {
  loadMaterial(material: MaterialDTO): Promise<{
    id: string;
    duration: RationalTime;
    width?: number;
    height?: number;
    loaded: boolean;
  }>;
  unloadMaterial(id: string): Promise<void>;
  getLoadedMaterials(): string[];
}

/**
 * ICanvasAdapter defines the interface to interact with the preview canvas/view port.
 */
export interface ICanvasAdapter {
  attachCanvas(container: HTMLDivElement | HTMLCanvasElement): void;
  detachCanvas(): void;
  resize(width: number, height: number): void;
  setCurrentTime(time: RationalTime): Promise<void>;
  getCurrentTime(): RationalTime;
  play(): void;
  pause(): void;
  isPlaying(): boolean;
  renderFrame(): void;
}

/**
 * ITimelineController defines abstract timeline manipulations, separating business actions
 * from visual presentation components.
 */
export interface ITimelineController {
  loadTimeline(timeline: TimelineDTO): void;
  getTimeline(): TimelineDTO;
  insertClip(trackId: string, clip: ClipDTO): void;
  removeClip(clipId: string): void;
  updateClipPosition(clipId: string, start: RationalTime, duration: RationalTime): void;
  snapTime(time: RationalTime, toleranceMs: number): RationalTime;
  zoom(scale: number): void;
}

/**
 * IEditorAdapter brings together the loader, canvas, and timeline controllers
 * to form a complete decoupled editing workspace adapter.
 */
export interface IEditorAdapter {
  loader: IMaterialLoader;
  canvas: ICanvasAdapter;
  timeline: ITimelineController;

  initialize(): Promise<void>;
  destroy(): Promise<void>;
}

/**
 * Base implementation class as a skeleton wrapper
 */
export class BaseEditorAdapter implements IEditorAdapter {
  constructor(
    public loader: IMaterialLoader,
    public canvas: ICanvasAdapter,
    public timeline: ITimelineController
  ) {}

  async initialize(): Promise<void> {
    // Boilerplate initializer
  }

  async destroy(): Promise<void> {
    this.canvas.detachCanvas();
  }
}
