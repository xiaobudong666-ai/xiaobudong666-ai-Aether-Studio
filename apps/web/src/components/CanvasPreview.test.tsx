import { describe, expect, test, vi } from "vitest";
import { render, screen } from "@testing-library/react";
import { RationalTime } from "@aether/contracts";
import { CanvasPreview } from "./CanvasPreview";

describe("真实素材预览", () => {
  test("renders an authenticated video source instead of a placeholder", () => {
    render(
      <CanvasPreview
        currentTime={new RationalTime(0, 24000)}
        onTimeChange={vi.fn()}
        timelineDuration={new RationalTime(24000, 24000)}
        previewMaterial={{
          id: "media-1",
          name: "测试素材.mp4",
          url: "/api/video-use/media/project-1/media-1",
          type: "video",
          contentType: "video/mp4",
          duration: { value: 24000, timescale: 24000 },
          sizeBytes: 1024,
        }}
      />,
    );

    const video = screen.getByLabelText("预览素材 测试素材.mp4") as HTMLVideoElement;
    expect(video.getAttribute("src")).toBe("/api/video-use/media/project-1/media-1");
    expect(screen.queryByText("暂无可预览素材")).toBeNull();
    expect(screen.getByText(/最终合成效果以渲染成片为准/)).toBeTruthy();
  });
});
