import { fireEvent, render, screen } from "@testing-library/react";
import { beforeEach, describe, expect, test } from "vitest";
import { ProjectDTO } from "@aether/contracts";
import { GenerationPanel } from "./GenerationPanel";

const project: ProjectDTO = {
  id: "project-1",
  name: "受治理生成测试",
  revision: 1,
  timeline: { version: "1.1", tracks: [] },
  materials: [],
  createdAt: "2026-08-25T00:00:00Z",
  updatedAt: "2026-08-25T00:00:00Z",
};

describe("GenerationPanel", () => {
  beforeEach(() => localStorage.clear());
  test("viewer only sees the read-only boundary", () => {
    render(<GenerationPanel role="viewer" tenantId="tenant-1" actorId="viewer-1" project={project} assetVersions={[]} />);
    expect(screen.getByText(/当前为只读权限/)).toBeTruthy();
    expect(screen.queryByRole("button", { name: "打开生成任务" })).toBeNull();
  });

  test("preflight, local generation and governed editor reference never claim adoption", () => {
    render(<GenerationPanel role="owner" tenantId="tenant-1" actorId="owner-1" project={project} assetVersions={[]} />);
    fireEvent.click(screen.getByRole("button", { name: "打开生成任务" }));
    fireEvent.change(screen.getByLabelText("生成提示词"), { target: { value: "一匹马穿过雨夜城市" } });
    fireEvent.change(screen.getByLabelText("权利快照编号"), { target: { value: "rights-1" } });
    fireEvent.click(screen.getByRole("button", { name: "执行生成预检" }));
    expect(screen.getByText(/预检通过：可提交/)).toBeTruthy();
    fireEvent.click(screen.getByRole("button", { name: "提交生成" }));
    expect(screen.getByText(/已进入本地队列/)).toBeTruthy();
    fireEvent.click(screen.getByRole("button", { name: "运行本地任务" }));
    expect(screen.getByText(/结果等待人工审阅/)).toBeTruthy();
    fireEvent.click(screen.getByRole("button", { name: "进入剪辑" }));
    expect(screen.getByText(/未自动采纳，也未写入最终时间线/)).toBeTruthy();
    expect(screen.getByText(/adopted=false/)).toBeTruthy();
  });

  test("restores a queued task after the panel is unmounted and reopened", () => {
    const props = { role: "owner" as const, tenantId: "tenant-1", actorId: "owner-1", project, assetVersions: [] };
    const first = render(<GenerationPanel {...props} />);
    fireEvent.click(screen.getByRole("button", { name: "打开生成任务" }));
    fireEvent.change(screen.getByLabelText("生成提示词"), { target: { value: "恢复任务" } });
    fireEvent.change(screen.getByLabelText("权利快照编号"), { target: { value: "rights-1" } });
    fireEvent.click(screen.getByRole("button", { name: "执行生成预检" }));
    fireEvent.click(screen.getByRole("button", { name: "提交生成" }));
    first.unmount();

    render(<GenerationPanel {...props} />);
    fireEvent.click(screen.getByRole("button", { name: "打开生成任务" }));
    expect(screen.getByText(/generation-task-1/)).toBeTruthy();
    expect(screen.getByText(/排队中/)).toBeTruthy();
  });
});
