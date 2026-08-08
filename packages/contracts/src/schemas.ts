import { z } from "zod";

// RationalTime schema for validation
export const RationalTimeSchema = z.object({
  value: z.number().int().safe(),
  timescale: z.number().int().safe().positive(),
});

// Timeline Clip schema (Canonical Timeline v1.1)
export const ClipSchema = z.object({
  id: z.string(),
  trackId: z.string(),
  materialId: z.string(),
  start: RationalTimeSchema,     // Start time in the timeline
  duration: RationalTimeSchema,  // Duration of the clip
  sourceIn: RationalTimeSchema,  // In-point in the source material
  volume: z.number().min(0).max(4).optional(),
  opacity: z.number().min(0).max(1).optional(),
  x: z.number().int().optional(),
  y: z.number().int().optional(),
  width: z.number().int().positive().max(7680).optional(),
  height: z.number().int().positive().max(4320).optional(),
  text: z.string().max(2000).optional(),
});

// Timeline Track schema
export const TrackSchema = z.object({
  id: z.string(),
  name: z.string(),
  type: z.enum(["video", "audio", "subtitle"]),
  clips: z.array(ClipSchema),
});

// Canonical Timeline v1.1 Schema
export const TimelineSchema = z.object({
  version: z.literal("1.1"),
  tracks: z.array(TrackSchema),
});

// Material schema
export const MaterialSchema = z.object({
  id: z.string(),
  name: z.string(),
  url: z.string(),
  type: z.enum(["video", "audio", "image"]),
  duration: RationalTimeSchema.optional(),
  sizeBytes: z.number().int().nonnegative().optional(),
});

// Project schemas with concurrency control
export const ProjectSchema = z.object({
  id: z.string(),
  name: z.string(),
  timeline: TimelineSchema,
  materials: z.array(MaterialSchema),
  revision: z.number().int().nonnegative(), // auto-save and concurrency conflict protocol
  createdAt: z.string(),
  updatedAt: z.string(),
});

export const CreateProjectSchema = z.object({
  name: z.string().min(1, "Project name cannot be empty"),
});

export const UpdateProjectSchema = z.object({
  name: z.string().optional(),
  timeline: TimelineSchema.optional(),
  materials: z.array(MaterialSchema).optional(),
  expectedRevision: z.number().int().nonnegative(), // Optimistic locking revision check
});

// DTO Schemas
export type RationalTimeDTO = z.infer<typeof RationalTimeSchema>;
export type ClipDTO = z.infer<typeof ClipSchema>;
export type TrackDTO = z.infer<typeof TrackSchema>;
export type TimelineDTO = z.infer<typeof TimelineSchema>;
export type MaterialDTO = z.infer<typeof MaterialSchema>;
export type ProjectDTO = z.infer<typeof ProjectSchema>;
export type CreateProjectDTO = z.infer<typeof CreateProjectSchema>;
export type UpdateProjectDTO = z.infer<typeof UpdateProjectSchema>;
