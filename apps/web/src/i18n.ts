type ApiErrorPayload = {
  detail?: {
    code?: string;
    message?: string;
  } | string;
};

const ROLE_LABELS: Record<string, string> = {
  owner: "所有者",
  editor: "编辑者",
  viewer: "只读成员",
};

const MATERIAL_TYPE_LABELS: Record<string, string> = {
  video: "视频",
  audio: "音频",
  image: "图片",
};

const TRACK_TYPE_LABELS: Record<string, string> = {
  video: "视频轨道",
  audio: "音频轨道",
  subtitle: "字幕轨道",
};

const TASK_STATUS_LABELS: Record<string, string> = {
  queued: "排队中",
  dispatching: "正在分派",
  processing: "渲染中",
  completed: "已完成",
  failed: "失败",
  pending: "等待中",
};

const API_ERROR_LABELS: Record<string, string> = {
  AUTH_REQUIRED: "请先登录后再继续操作。",
  SESSION_EXPIRED: "登录已过期，请重新登录。",
  INVALID_CREDENTIALS: "邮箱或密码不正确。",
  PERMISSION_DENIED: "当前账号没有执行此操作的权限。",
  CSRF_REQUIRED: "请求校验失败，请刷新页面后重试。",
  PROJECT_NOT_FOUND: "未找到该项目，可能已被删除。",
  PROJECT_QUOTA_EXCEEDED: "项目数量已达到当前配额上限。",
  CONCURRENCY_CONFLICT: "项目已在其他页面更新，已为你重新加载最新版本。",
  MATERIALS_SERVER_MANAGED: "素材只能通过素材上传功能进行修改。",
  EMPTY_UPLOAD: "所选文件为空，请重新选择媒体文件。",
  UPLOAD_TOO_LARGE: "媒体文件超过系统允许的上传大小。",
  STORAGE_QUOTA_EXCEEDED: "团队存储空间已用完，请清理素材或提升配额。",
  VIDEO_USE_UPLOAD_FAILED: "媒体处理服务暂时无法完成上传，请稍后重试。",
  MEDIA_NOT_FOUND: "未找到该媒体文件。",
  NO_RENDERABLE_VIDEO: "请先上传视频并将其添加到视频轨道。",
  EMPTY_TIMELINE: "时间线没有可渲染的内容。",
  RENDER_CONCURRENCY_QUOTA_EXCEEDED: "同时渲染的任务数已达到上限，请等待已有任务完成。",
  RENDER_SECONDS_QUOTA_EXCEEDED: "本月可用渲染时长已用完。",
  ARTIFACT_NOT_FOUND: "成片尚不可下载或已经失效。",
  TASK_NOT_FOUND: "未找到该任务。",
  EMAIL_EXISTS: "该邮箱已经存在。",
  VIDEO_USE_UNAVAILABLE: "视频处理服务暂时不可用。",
};

export function roleLabel(role: string): string {
  return ROLE_LABELS[role] || "未知角色";
}

export function materialTypeLabel(type: string): string {
  return MATERIAL_TYPE_LABELS[type] || "未知类型";
}

export function trackTypeLabel(type: string): string {
  return TRACK_TYPE_LABELS[type] || "未知轨道";
}

export function localizeTrackName(name: string, type: string): string {
  const systemTrack = /^(Video|Audio|Subtitle) Track (\d+)$/i.exec(name);
  if (systemTrack) return `${trackTypeLabel(type)} ${systemTrack[2]}`;
  return name;
}

export function taskStatusLabel(status: string): string {
  return TASK_STATUS_LABELS[status] || "状态未知";
}

export function taskMessageLabel(status: string): string {
  switch (status) {
    case "queued":
      return "任务已进入渲染队列。";
    case "dispatching":
      return "正在分配渲染资源。";
    case "processing":
      return "正在生成成片，请保持页面开启或稍后回来查看。";
    case "completed":
      return "成片已生成，可以下载。";
    case "failed":
      return "渲染失败，请检查素材后重试；技术详情已记录到服务日志。";
    default:
      return "任务状态正在更新。";
  }
}

export function apiErrorMessage(payload: unknown, fallback: string): string {
  if (!payload || typeof payload !== "object") return fallback;
  const detail = (payload as ApiErrorPayload).detail;
  if (!detail || typeof detail === "string") return fallback;
  if (detail.code && API_ERROR_LABELS[detail.code]) return API_ERROR_LABELS[detail.code];
  if (detail.message && /[\u3400-\u9fff]/.test(detail.message)) return detail.message;
  return fallback;
}

export function safeErrorMessage(error: unknown, fallback: string): string {
  if (error instanceof Error && /[\u3400-\u9fff]/.test(error.message)) return error.message;
  return fallback;
}

export function formatBytes(bytes: number): string {
  if (!Number.isFinite(bytes) || bytes <= 0) return "0 B";
  const units = ["B", "KB", "MB", "GB", "TB"];
  const index = Math.min(Math.floor(Math.log(bytes) / Math.log(1024)), units.length - 1);
  const value = bytes / 1024 ** index;
  return `${value >= 10 || index === 0 ? value.toFixed(0) : value.toFixed(1)} ${units[index]}`;
}
