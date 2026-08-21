import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { beforeEach, describe, expect, test, vi } from "vitest";
import { ComponentProps } from "react";
import { AssetVersionDTO, ProjectDTO, RenderTaskDTO, RightsCheckDTO } from "@aether/contracts";
import { QuickCreatePanel, QuickProjectSnapshot } from "./QuickCreatePanel";

const project: ProjectDTO = {
  id: "project-1",
  name: "快速制作测试",
  revision: 1,
  timeline: { version: "1.1", tracks: [] },
  materials: [],
  createdAt: "2026-08-21T00:00:00Z",
  updatedAt: "2026-08-21T00:00:00Z",
};

const version: AssetVersionDTO = {
  id: "version-1",
  projectId: project.id,
  mediaId: "media-1",
  versionNo: 1,
  sha256: "a".repeat(64),
  mediaType: "video",
  contentType: "video/mp4",
  sizeBytes: 100,
  probe: { durationSeconds: 2 },
  createdBy: "owner-1",
  createdAt: "2026-08-21T00:01:00Z",
};

const uploadedProject: ProjectDTO = {
  ...project,
  revision: 2,
  materials: [{
    id: "media-1",
    name: "source.mp4",
    url: "/api/media/media-1",
    type: "video",
    contentType: "video/mp4",
    sizeBytes: 100,
    duration: { value: 48, timescale: 24 },
  }],
};

const allowed: RightsCheckDTO = {
  assetVersionId: version.id,
  allowed: true,
  code: "RIGHTS_ALLOWED",
  snapshot: {
    id: "rights-1",
    assetVersionId: version.id,
    status: "ALLOWED",
    purpose: "EXPORT",
    territory: "GLOBAL",
    validFrom: null,
    validUntil: null,
    evidenceRef: "evidence://test",
    capturedBy: "owner-1",
    capturedAt: "2026-08-21T00:02:00Z",
  },
};

const missing: RightsCheckDTO = {
  assetVersionId: version.id,
  allowed: false,
  code: "RIGHTS_MISSING",
  snapshot: null,
};

function setup(overrides: Partial<ComponentProps<typeof QuickCreatePanel>> = {}) {
  const saveTimeline = vi.fn(async (source: ProjectDTO, timeline: ProjectDTO["timeline"]) => ({
    ...source,
    timeline,
    revision: source.revision + 1,
  }));
  const submitRender = vi.fn(async () => ({
    taskId: "task-1",
    projectId: project.id,
    progress: 0,
    status: "queued",
    canonicalStatus: "QUEUED" as const,
    message: "queued",
  }));
  const props: ComponentProps<typeof QuickCreatePanel> = {
    role: "owner",
    currentProject: project,
    assetVersions: [],
    busy: false,
    createProject: vi.fn(),
    reloadProject: vi.fn(async (): Promise<QuickProjectSnapshot> => ({ project: uploadedProject, assetVersions: [version] })),
    uploadMedia: vi.fn(async () => ({ project: uploadedProject, assetVersion: version })),
    checkRights: vi.fn(async () => allowed),
    saveTimeline,
    submitRender,
    refreshRenderTasks: vi.fn(async () => []),
    isProjectActive: vi.fn(() => true),
    onOpenGovernance: vi.fn(),
    onViewTask: vi.fn(),
    onViewFinished: vi.fn(),
    ...overrides,
  };
  const renderResult = render(<QuickCreatePanel {...props} />);
  return { props, saveTimeline, submitRender, renderResult };
}

async function openAndChooseFile() {
  fireEvent.click(screen.getByRole("button", { name: "快速制作短视频" }));
  const file = new File(["video"], "source.mp4", { type: "video/mp4" });
  fireEvent.change(screen.getByLabelText("快速制作媒体文件"), { target: { files: [file] } });
  fireEvent.click(screen.getByLabelText(/确认通过后会创建真实渲染任务/));
  fireEvent.click(screen.getByRole("button", { name: "执行预检" }));
  await screen.findByText("状态：可以生成");
}

describe("QuickCreatePanel", () => {
  beforeEach(() => vi.clearAllMocks());

  test("viewer sees a read-only boundary and no write entry", () => {
    setup({ role: "viewer" });
    expect(screen.getByText(/当前为只读权限/)).toBeTruthy();
    expect(screen.queryByRole("button", { name: "快速制作短视频" })).toBeNull();
  });

  test("new upload without rights blocks with zero timeline save and zero render", async () => {
    const checkRights = vi.fn(async () => missing);
    const { props, saveTimeline, submitRender } = setup({ checkRights });
    await openAndChooseFile();
    fireEvent.click(screen.getByRole("button", { name: "一键生成短视频" }));

    await screen.findByText("状态：已阻断");
    expect(screen.getByRole("alert").textContent).toContain("未保存时间线");
    expect(props.uploadMedia).toHaveBeenCalledTimes(1);
    expect(checkRights).toHaveBeenCalledTimes(1);
    expect(saveTimeline).not.toHaveBeenCalled();
    expect(submitRender).not.toHaveBeenCalled();

    fireEvent.click(screen.getByRole("button", { name: "前往素材治理" }));
    expect(props.onOpenGovernance).toHaveBeenCalledTimes(1);
    expect(screen.getByRole("button", { name: "返回快速制作" })).toBeTruthy();
    fireEvent.click(screen.getByRole("button", { name: "返回快速制作" }));
    expect(screen.getByRole("button", { name: "重新预检并继续" })).toBeTruthy();
  });

  test("resume after explicit governance reuses upload and submits one deterministic timeline", async () => {
    let rightsAllowed = false;
    const checkRights = vi.fn(async () => rightsAllowed ? allowed : missing);
    const { props, saveTimeline, submitRender } = setup({ checkRights });
    await openAndChooseFile();
    fireEvent.click(screen.getByRole("button", { name: "一键生成短视频" }));
    await screen.findByText("状态：已阻断");

    rightsAllowed = true;
    fireEvent.click(screen.getByRole("button", { name: "重新预检并继续" }));
    await screen.findByText("状态：任务已提交");

    expect(props.uploadMedia).toHaveBeenCalledTimes(1);
    expect(saveTimeline).toHaveBeenCalledTimes(1);
    expect(submitRender).toHaveBeenCalledTimes(1);
    const timeline = saveTimeline.mock.calls[0][1];
    expect(timeline.version).toBe("1.1");
    expect(timeline.tracks).toHaveLength(1);
    expect(timeline.tracks[0].clips[0]).toMatchObject({
      materialId: "media-1",
      start: { value: 0, timescale: 24000 },
      duration: { value: 48000, timescale: 24000 },
      sourceIn: { value: 0, timescale: 24000 },
    });
  });

  test("double click cannot submit a second render", async () => {
    let resolveRender!: (value: RenderTaskDTO) => void;
    const submitRender = vi.fn(() => new Promise<RenderTaskDTO>((resolve) => { resolveRender = resolve; }));
    setup({ submitRender });
    await openAndChooseFile();
    const generate = screen.getByRole("button", { name: "一键生成短视频" });
    fireEvent.click(generate);
    fireEvent.click(generate);
    await waitFor(() => expect(submitRender).toHaveBeenCalledTimes(1));
    resolveRender({ taskId: "task-1", projectId: project.id, progress: 0, status: "queued", message: "queued" });
    await screen.findByText("状态：任务已提交");
  });

  test("two governed existing media keep order, fixed duration, and one save", async () => {
    const secondVersion = { ...version, id: "version-2", mediaId: "media-2", sha256: "b".repeat(64) };
    const governedProject: ProjectDTO = {
      ...project,
      materials: [
        { ...uploadedProject.materials[0], id: "media-1", name: "first.mp4", duration: { value: 48, timescale: 24 } },
        { ...uploadedProject.materials[0], id: "media-2", name: "second.mp4", duration: { value: 48, timescale: 24 } },
      ],
    };
    const reloadProject = vi.fn(async () => ({ project: governedProject, assetVersions: [version, secondVersion] }));
    const { saveTimeline } = setup({
      currentProject: governedProject,
      assetVersions: [version, secondVersion],
      reloadProject,
      checkRights: vi.fn(async (_projectId, assetVersionId) => ({ ...allowed, assetVersionId })),
    });
    fireEvent.click(screen.getByRole("button", { name: "快速制作短视频" }));
    fireEvent.click(screen.getByLabelText("first.mp4"));
    fireEvent.click(screen.getByLabelText("second.mp4"));
    fireEvent.click(screen.getByLabelText("统一时长"));
    fireEvent.change(screen.getByLabelText("统一片段时长"), { target: { value: "3" } });
    fireEvent.click(screen.getByLabelText(/确认通过后会创建真实渲染任务/));
    fireEvent.click(screen.getByRole("button", { name: "执行预检" }));
    await screen.findByText("状态：可以生成");
    fireEvent.click(screen.getByRole("button", { name: "一键生成短视频" }));
    await screen.findByText("状态：任务已提交");

    expect(saveTimeline).toHaveBeenCalledTimes(1);
    const clips = saveTimeline.mock.calls[0][1].tracks[0].clips;
    expect(clips.map((clip) => clip.materialId)).toEqual(["media-1", "media-2"]);
    expect(clips.map((clip) => clip.start.value)).toEqual([0, 48000]);
    expect(clips.map((clip) => clip.duration.value)).toEqual([48000, 48000]);
  });

  test("existing timeline requires explicit replacement before any write", async () => {
    const projectWithTimeline: ProjectDTO = {
      ...uploadedProject,
      timeline: {
        version: "1.1",
        tracks: [{
          id: "track-existing",
          name: "视频轨道 1",
          type: "video",
          clips: [{
            id: "clip-existing",
            trackId: "track-existing",
            materialId: "media-1",
            start: { value: 0, timescale: 24000 },
            duration: { value: 24000, timescale: 24000 },
            sourceIn: { value: 0, timescale: 24000 },
          }],
        }],
      },
    };
    const { saveTimeline, submitRender } = setup({ currentProject: projectWithTimeline, assetVersions: [version] });
    fireEvent.click(screen.getByRole("button", { name: "快速制作短视频" }));
    fireEvent.click(screen.getByLabelText("source.mp4"));
    fireEvent.click(screen.getByRole("button", { name: "执行预检" }));
    await screen.findByText("状态：已阻断");
    expect(screen.getByRole("alert").textContent).toContain("确认覆盖");
    expect(saveTimeline).not.toHaveBeenCalled();
    expect(submitRender).not.toHaveBeenCalled();
  });

  test("second upload failure reports partial completion and never saves or renders", async () => {
    const uploadMedia = vi.fn()
      .mockResolvedValueOnce({ project: uploadedProject, assetVersion: version })
      .mockRejectedValueOnce(new Error("第二个素材上传失败"));
    const { saveTimeline, submitRender } = setup({ uploadMedia });
    fireEvent.click(screen.getByRole("button", { name: "快速制作短视频" }));
    fireEvent.change(screen.getByLabelText("快速制作媒体文件"), {
      target: { files: [
        new File(["one"], "one.mp4", { type: "video/mp4" }),
        new File(["two"], "two.mp4", { type: "video/mp4" }),
      ] },
    });
    fireEvent.click(screen.getByLabelText(/确认通过后会创建真实渲染任务/));
    fireEvent.click(screen.getByRole("button", { name: "执行预检" }));
    await screen.findByText("状态：可以生成");
    fireEvent.click(screen.getByRole("button", { name: "一键生成短视频" }));
    await screen.findByText("状态：部分完成");
    expect(screen.getByRole("alert").textContent).toContain("one.mp4");
    expect(screen.getByRole("alert").textContent).toContain("不会被伪造回滚");
    expect(saveTimeline).not.toHaveBeenCalled();
    expect(submitRender).not.toHaveBeenCalled();
  });

  test("concurrency conflict stops without render", async () => {
    const saveTimeline = vi.fn(async () => { throw new Error("项目发生并发冲突，请重新预检"); });
    const { submitRender } = setup({ saveTimeline });
    await openAndChooseFile();
    fireEvent.click(screen.getByRole("button", { name: "一键生成短视频" }));
    await screen.findByText("状态：并发冲突");
    expect(submitRender).not.toHaveBeenCalled();
  });

  test("unknown POST response refreshes tasks and never retries render automatically", async () => {
    const submitRender = vi.fn(async () => { throw new Error("网络响应未知"); });
    const refreshRenderTasks = vi.fn()
      .mockResolvedValueOnce([])
      .mockResolvedValueOnce([]);
    setup({ submitRender, refreshRenderTasks });
    await openAndChooseFile();
    fireEvent.click(screen.getByRole("button", { name: "一键生成短视频" }));
    await screen.findByText("状态：提交结果待确认");
    expect(submitRender).toHaveBeenCalledTimes(1);
    expect(refreshRenderTasks).toHaveBeenCalledTimes(2);
    expect(screen.queryByRole("button", { name: "查看候选成片" })).toBeNull();
  });

  test("late rights response from a previous project cannot make the new project ready", async () => {
    let activeProjectId = project.id;
    let resolveRights!: (value: RightsCheckDTO) => void;
    const checkRights = vi.fn(() => new Promise<RightsCheckDTO>((resolve) => { resolveRights = resolve; }));
    const governedProject = { ...uploadedProject };
    const { props, renderResult, saveTimeline, submitRender } = setup({
      currentProject: governedProject,
      assetVersions: [version],
      checkRights,
      isProjectActive: (projectId) => projectId === activeProjectId,
    });
    fireEvent.click(screen.getByRole("button", { name: "快速制作短视频" }));
    fireEvent.click(screen.getByLabelText("source.mp4"));
    fireEvent.click(screen.getByRole("button", { name: "执行预检" }));
    await waitFor(() => expect(checkRights).toHaveBeenCalledTimes(1));

    const nextProject: ProjectDTO = { ...project, id: "project-2", name: "第二项目" };
    activeProjectId = nextProject.id;
    renderResult.rerender(<QuickCreatePanel {...props} currentProject={nextProject} assetVersions={[]} />);
    resolveRights(allowed);

    await screen.findByText("项目已切换，请重新选择素材并预检。");
    expect(screen.getByText("状态：编辑中")).toBeTruthy();
    expect(screen.queryByText("状态：可以生成")).toBeNull();
    expect(saveTimeline).not.toHaveBeenCalled();
    expect(submitRender).not.toHaveBeenCalled();
  });
});
