import { describe, expect, test } from "vitest";
import {
  apiErrorMessage,
  localizeTrackName,
  materialTypeLabel,
  roleLabel,
  safeErrorMessage,
  taskMessageLabel,
  taskStatusLabel,
} from "./i18n";

describe("中文文案映射", () => {
  test("maps roles, media, tracks, tasks, and API errors", () => {
    expect(roleLabel("owner")).toBe("所有者");
    expect(materialTypeLabel("video")).toBe("视频");
    expect(localizeTrackName("Video Track 1", "video")).toBe("视频轨道 1");
    expect(taskStatusLabel("completed")).toBe("已完成");
    expect(taskMessageLabel("failed")).toContain("渲染失败");
    expect(apiErrorMessage({ detail: { code: "UPLOAD_TOO_LARGE" } }, "失败")).toContain("上传大小");
  });

  test("does not leak unknown English service errors into the interface", () => {
    expect(apiErrorMessage({ detail: { message: "upstream connection refused" } }, "服务异常")).toBe("服务异常");
    expect(safeErrorMessage(new Error("network failed"), "网络异常")).toBe("网络异常");
  });
});
