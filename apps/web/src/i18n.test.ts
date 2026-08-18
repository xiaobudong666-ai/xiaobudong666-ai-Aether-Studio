import { describe, expect, test } from "vitest";
import {
  apiErrorMessage,
  candidateStatusLabel,
  localizeTrackName,
  materialTypeLabel,
  roleLabel,
  rightsDecisionLabel,
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
    expect(taskStatusLabel("UNKNOWN")).toBe("状态待确认");
    expect(taskMessageLabel("failed")).toContain("渲染失败");
    expect(taskMessageLabel("PARTIAL")).toContain("部分结果");
    expect(rightsDecisionLabel("RIGHTS_EXPIRED")).toBe("权利已过期");
    expect([
      "RIGHTS_ALLOWED",
      "RIGHTS_MISSING",
      "RIGHTS_DENIED",
      "RIGHTS_REVOKED",
      "RIGHTS_UNKNOWN",
      "RIGHTS_NOT_YET_VALID",
      "RIGHTS_EXPIRED",
    ].map(rightsDecisionLabel)).not.toContain("权利状态待确认");
    expect(candidateStatusLabel("READY")).toBe("待采纳");
    expect(apiErrorMessage({ detail: { code: "UPLOAD_TOO_LARGE" } }, "失败")).toContain("上传大小");
  });

  test("does not leak unknown English service errors into the interface", () => {
    expect(apiErrorMessage({ detail: { message: "upstream connection refused" } }, "服务异常")).toBe("服务异常");
    expect(safeErrorMessage(new Error("network failed"), "网络异常")).toBe("网络异常");
  });
});
