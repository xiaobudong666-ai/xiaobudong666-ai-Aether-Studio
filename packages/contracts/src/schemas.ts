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
  contentType: z.string().optional(),
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
  name: z.string().min(1, "项目名称不能为空"),
});

export const UpdateProjectSchema = z.object({
  name: z.string().optional(),
  timeline: TimelineSchema.optional(),
  materials: z.array(MaterialSchema).optional(),
  expectedRevision: z.number().int().nonnegative(), // Optimistic locking revision check
});

export const CanonicalTaskStatusSchema = z.enum([
  "QUEUED",
  "RUNNING",
  "SUCCEEDED",
  "FAILED",
  "CANCELED",
  "PARTIAL",
  "UNKNOWN",
]);

const legacyTaskStatuses = {
  queued: "QUEUED",
  dispatching: "RUNNING",
  processing: "RUNNING",
  completed: "SUCCEEDED",
  failed: "FAILED",
  canceled: "CANCELED",
  cancelled: "CANCELED",
  partial: "PARTIAL",
  unknown: "UNKNOWN",
} as const;

export function canonicalTaskStatus(status: string): CanonicalTaskStatusDTO {
  const canonical = (
    status in legacyTaskStatuses
      ? legacyTaskStatuses[status as keyof typeof legacyTaskStatuses]
      : status
  );
  return CanonicalTaskStatusSchema.parse(canonical);
}

export const AssetVersionSchema = z.object({
  id: z.string().min(1),
  projectId: z.string().min(1),
  mediaId: z.string().min(1),
  versionNo: z.number().int().positive(),
  sha256: z.string().regex(/^[a-f0-9]{64}$/),
  mediaType: z.enum(["video", "audio", "image"]),
  contentType: z.string().nullable(),
  sizeBytes: z.number().int().nonnegative(),
  probe: z.record(z.unknown()),
  createdBy: z.string().min(1),
  createdAt: z.string().min(1),
});

export const RightsSnapshotSchema = z.object({
  id: z.string().min(1),
  assetVersionId: z.string().min(1),
  status: z.enum(["ALLOWED", "DENIED", "REVOKED", "UNKNOWN"]),
  purpose: z.string().min(1),
  territory: z.string().min(1),
  validFrom: z.string().nullable(),
  validUntil: z.string().nullable(),
  evidenceRef: z.string().nullable(),
  capturedBy: z.string().min(1),
  capturedAt: z.string().min(1),
});

export const CandidateSchema = z.object({
  id: z.string().min(1),
  projectId: z.string().min(1),
  taskId: z.string().min(1),
  artifactRef: z.string().min(1),
  inputRevision: z.number().int().positive(),
  status: z.enum(["READY", "ADOPTED"]),
  createdAt: z.string().min(1),
});

export const MasterRevisionSchema = z.object({
  id: z.string().min(1),
  projectId: z.string().min(1),
  revisionNo: z.number().int().positive(),
  artifactRef: z.string().min(1),
  sha256: z.string().regex(/^[a-f0-9]{64}$/).nullable(),
  createdAt: z.string().min(1),
  adoption: z.object({
    id: z.string().min(1),
    candidateId: z.string().min(1),
    adoptedBy: z.string().min(1),
    adoptedAt: z.string().min(1),
    reason: z.string().min(1),
    supersedesId: z.string().nullable(),
  }),
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
export type CanonicalTaskStatusDTO = z.infer<typeof CanonicalTaskStatusSchema>;
export type AssetVersionDTO = z.infer<typeof AssetVersionSchema>;
export type RightsSnapshotDTO = z.infer<typeof RightsSnapshotSchema>;
export type CandidateDTO = z.infer<typeof CandidateSchema>;
export type MasterRevisionDTO = z.infer<typeof MasterRevisionSchema>;
