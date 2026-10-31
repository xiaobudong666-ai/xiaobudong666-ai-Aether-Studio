import { expect, test } from "@playwright/test";
import { deflateSync } from "node:zlib";

const password = process.env.AETHER_PLAYWRIGHT_PASSWORD;
if (!password) throw new Error("AETHER_PLAYWRIGHT_PASSWORD must be set to the disposable Compose admin password");

const apiBase = "http://127.0.0.1:5173/api";

function crc32(buffer) {
  let crc = 0xffffffff;
  for (const byte of buffer) {
    crc ^= byte;
    for (let bit = 0; bit < 8; bit += 1) {
      crc = (crc >>> 1) ^ ((crc & 1) ? 0xedb88320 : 0);
    }
  }
  return (crc ^ 0xffffffff) >>> 0;
}

function pngChunk(type, data) {
  const typeBytes = Buffer.from(type, "ascii");
  const length = Buffer.alloc(4);
  length.writeUInt32BE(data.length, 0);
  const checksum = Buffer.alloc(4);
  checksum.writeUInt32BE(crc32(Buffer.concat([typeBytes, data])), 0);
  return Buffer.concat([length, typeBytes, data, checksum]);
}

function syntheticPng(width = 48, height = 48) {
  const stride = width * 4 + 1;
  const raw = Buffer.alloc(stride * height);
  for (let y = 0; y < height; y += 1) {
    const row = y * stride;
    raw[row] = 0;
    for (let x = 0; x < width; x += 1) {
      const offset = row + 1 + x * 4;
      raw[offset] = 72;
      raw[offset + 1] = 128;
      raw[offset + 2] = 184;
      raw[offset + 3] = 255;
    }
  }
  const header = Buffer.alloc(13);
  header.writeUInt32BE(width, 0);
  header.writeUInt32BE(height, 4);
  header[8] = 8;
  header[9] = 6;
  header[10] = 0;
  header[11] = 0;
  header[12] = 0;
  return Buffer.concat([
    Buffer.from([137, 80, 78, 71, 13, 10, 26, 10]),
    pngChunk("IHDR", header),
    pngChunk("IDAT", deflateSync(raw)),
    pngChunk("IEND", Buffer.alloc(0)),
  ]);
}

async function signIn(page) {
  await page.getByLabel("邮箱").fill("admin@aether.local");
  await page.getByLabel("密码").fill(password);
  await page.getByRole("button", { name: "登录" }).click();
  await expect(page.getByRole("heading", { name: "输入内容" })).toBeVisible();
}

async function apiJson(page, path) {
  const response = await page.request.get(`${apiBase}${path}`);
  expect(response.ok(), `${path} returned ${response.status()}`).toBeTruthy();
  return response.json();
}

test("G72 normal UI entry reaches the isolated FAKE Worker and keeps generated export denied", async ({ page }, testInfo) => {
  test.setTimeout(420_000);
  await page.goto("/");
  await signIn(page);

  const projectName = `G72-UI-Worker-${Date.now()}`;
  await page.getByPlaceholder("输入新项目名称").fill(projectName);
  await page.getByRole("button", { name: "创建项目" }).click();

  const projectOption = page.getByRole("option", { name: new RegExp(`^${projectName}（版本 1）$`) });
  await expect(projectOption).toHaveCount(1);
  const projectId = await projectOption.getAttribute("value");
  expect(projectId).toBeTruthy();

  const project = await apiJson(page, `/projects/${projectId}`);
  const upload = await page.request.post(`${apiBase}/projects/${projectId}/media`, {
    headers: { "X-Aether-CSRF": "1" },
    multipart: {
      expectedRevision: String(project.revision),
      file: {
        name: "g72-ui-normal-entry.png",
        mimeType: "image/png",
        buffer: syntheticPng(),
      },
    },
  });
  const uploadText = await upload.text();
  expect(upload.status(), uploadText).toBe(201);
  const uploaded = JSON.parse(uploadText);
  const assetVersionId = uploaded.assetVersion?.id;
  expect(assetVersionId).toBeTruthy();

  const remixRights = await page.request.post(
    `${apiBase}/projects/${projectId}/asset-versions/${assetVersionId}/rights-snapshots`,
    {
      headers: { "X-Aether-CSRF": "1" },
      data: {
        status: "ALLOWED",
        purpose: "REMIX",
        territory: "GLOBAL",
        evidenceRef: "synthetic-g72-ui-ci-remix-fixture",
      },
    },
  );
  expect(remixRights.status(), await remixRights.text()).toBe(201);

  const capabilities = await apiJson(page, "/generation/providers/moneyprinter/capabilities");
  expect(capabilities.mode).toBe("deterministic-fake");
  expect(capabilities.enabled).toBe(true);

  await page.reload();
  await expect(page.getByRole("heading", { name: "输入内容" })).toBeVisible();
  await page.getByLabel("选择项目").selectOption(projectId);
  await page.getByLabel("口播文案").fill("仅用于 G72 浏览器到 Worker 的非生产 FAKE 路由验收。");
  await page.getByRole("button", { name: "从项目素材选择人物照片" }).click();
  const assetPicker = page.getByLabel("选择人物图片素材");
  await expect(assetPicker.locator(`option[value="${assetVersionId}"]`)).toHaveCount(1);
  await assetPicker.selectOption(assetVersionId);

  const submit = page.getByRole("button", { name: "生成口播测试样片（FAKE-ONLY）" });
  await expect(submit).toBeEnabled();
  const createResponsePromise = page.waitForResponse((response) => (
    response.url().endsWith("/talking-head/fake-generation-tasks")
    && response.request().method() === "POST"
  ));
  await submit.click();
  const createResponse = await createResponsePromise;
  const createText = await createResponse.text();
  expect(createResponse.status(), createText).toBe(202);
  const created = JSON.parse(createText);
  expect(created.taskId).toBeTruthy();
  await expect(page.getByRole("status")).toContainText("FAKE-only 测试任务已入队");

  await expect.poll(async () => {
    const response = await page.request.get(
      `${apiBase}/projects/${projectId}/generation-tasks/${created.taskId}`,
    );
    if (!response.ok()) return `HTTP_${response.status()}`;
    const task = await response.json();
    const status = task.storedStatus || task.status;
    if (["FAILED", "CANCELED", "UNKNOWN"].includes(status)) {
      return `${status}:${task.errorCode || "NO_ERROR_CODE"}`;
    }
    return status;
  }, { timeout: 360_000, intervals: [3_000] }).toBe("RIGHTS_BLOCKED");

  const finalTask = await apiJson(page, `/projects/${projectId}/generation-tasks/${created.taskId}`);
  expect(finalTask.storedStatus).toBe("RIGHTS_BLOCKED");
  expect(finalTask.rights).toEqual({ allowed: false, code: "RIGHTS_MISSING" });
  expect(finalTask.attempts).toHaveLength(1);
  expect(finalTask.attempts[0].reconciliationState).toBe("ARTIFACT_INGESTED");
  expect(finalTask.events.some((event) => event.type === "TASK_CREATED"
    && event.metadata?.fakeOnly === true
    && event.metadata?.simulatedOutput === true)).toBe(true);
  expect(finalTask.events.some((event) => event.type === "TASK_CLAIMED"
    && event.actorType === "WORKER")).toBe(true);
  expect(finalTask.results).toHaveLength(1);
  expect(finalTask.results[0].provenance).toMatchObject({
    provider: "deterministic-fake-talking-head",
    generationTaskId: created.taskId,
    attempt: 1,
  });
  expect(finalTask.events.map((event) => event.type)).toContain("ARTIFACT_INGESTED");

  const mediaId = finalTask.results[0].mediaId;
  const exportAttempt = await page.request.get(`${apiBase}/video-use/media/${projectId}/${mediaId}`);
  expect(exportAttempt.status()).toBe(403);
  expect(await exportAttempt.json()).toMatchObject({
    detail: { code: "RIGHTS_MISSING" },
  });
  await page.screenshot({ path: testInfo.outputPath("g72-normal-entry-worker-complete.png"), fullPage: true });
  console.log(JSON.stringify({
    gate: "G72_NORMAL_UI_TO_WORKER_FAKE_ONLY",
    projectId,
    taskId: created.taskId,
    storedStatus: finalTask.storedStatus,
    provider: finalTask.results[0].provenance.provider,
    artifactSha256: finalTask.results[0].checksum,
    exportDenied: exportAttempt.status() === 403,
  }));
});
