// @vitest-environment jsdom
import {
  act,
  cleanup,
  fireEvent,
  render,
  renderHook,
  screen,
  waitFor,
} from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, test, vi } from "vitest";
import {
  focusManager,
  QueryClient,
  QueryClientProvider,
} from "@tanstack/react-query";
import { ReactFlowProvider } from "@xyflow/react";
import { MemoryRouter } from "react-router-dom";
import { AxiosError } from "axios";

import * as axiosClientModule from "@/api/AxiosClient";
import * as toastModule from "@/components/ui/use-toast";
import { useWorkflowHasChangesStore } from "@/store/WorkflowHasChangesStore";
import { useWorkflowHistoryAccessStore } from "@/store/WorkflowHistoryAccessStore";
import { useWorkflowSnapshotStore } from "@/store/WorkflowSnapshotStore";
import { useWorkflowYamlEditorStore } from "@/store/WorkflowYamlEditorStore";

import type {
  WorkflowBlock,
  WorkflowBlockImage,
} from "../../types/workflowTypes";
import { getElements, getWorkflowBlocks } from "../workflowEditorUtils";
import { WorkflowScopeContext } from "../WorkflowScopeContext";
import type { DocGraph, DocNoteRef } from "./docGraphEdits";
import type { AppNode } from "../nodes";
import {
  docImageRefusal,
  docImageScales,
  fitDocImage,
  localImageUrl,
  rememberLocalImage,
  releaseDocImageUploads,
  useDocImageUploadScope,
  useDocImageUploadStore,
} from "./docImageUploads";
import { createDocEditApi, type DocGraphEdits } from "./docEditApi";
import { useDocImageFiles } from "./useDocImageFiles";
import { isDocImageHandle } from "./docImageHandles";
import { WorkflowDocActionsContext } from "./WorkflowDocActionsContext";
import { clearDocAnnouncement, docAnnouncement } from "./docAnnounce";
import { block, code, forLoop, settings } from "./docTestBlocks";
import { WorkflowDocEditor } from "./WorkflowDocEditor";

vi.mock("./useDocLatestRunCompleted", () => ({
  useDocLatestRunCompleted: () => false,
}));
vi.mock("./DocRunStepButton", () => ({ DocRunStepButton: () => null }));
vi.mock("../panels/BlockConfigForm", () => ({ BlockConfigForm: () => null }));
const imagesFlag = vi.hoisted(() => ({ on: true }));
vi.mock("@/hooks/useWorkflowDocEditorFlag", async (original) => ({
  ...(await original<object>()),
  useWorkflowDocImagesFlag: () => imagesFlag.on,
}));

const post = vi.fn();
const get = vi.fn();

beforeEach(() => {
  // The Doc checks the draft as it stands; that request is not an upload.
  vi.spyOn(axiosClientModule, "getClient").mockResolvedValue({
    post: (url: string, ...rest: unknown[]) =>
      url === "/workflows/validate"
        ? Promise.resolve({ data: { problems: [] } })
        : post(url, ...rest),
    get,
  } as never);
  clearDocAnnouncement();
  URL.createObjectURL = vi.fn(() => "blob:local-shot");
  URL.revokeObjectURL = vi.fn();
  vi.stubGlobal(
    "ResizeObserver",
    class {
      observe() {}
      unobserve() {}
      disconnect() {}
    },
  );
});

afterEach(() => {
  cleanup();
  rerenderDoc = null;
  releaseDocImageUploads();
  post.mockReset();
  get.mockReset();
  vi.restoreAllMocks();
  vi.unstubAllGlobals();
  useWorkflowHistoryAccessStore.getState().reset();
  useWorkflowHasChangesStore.setState({
    hasChanges: false,
    saveIsPending: false,
    getSaveData: () => null,
  });
  useWorkflowYamlEditorStore.setState({ active: false, stale: false });
  useWorkflowSnapshotStore.getState().clearSnapshot();
  imagesFlag.on = true;
});

const image = (
  fileId: string,
  placement: WorkflowBlockImage["placement"] = "above",
): WorkflowBlockImage => ({ file_id: fileId, caption: null, placement });

// A live Doc with an undo stack: captureImmediately records the graph, undo puts the last record back.
function renderImageDoc(
  extra: Record<string, unknown>,
  make: (label: string, extra: Record<string, unknown>) => WorkflowBlock = (
    label,
    data,
  ) => code(label, null, data),
) {
  let current = getElements(
    [make("first", { notes: "Read the plan", ...extra })],
    settings(),
    true,
  ) as DocGraph;
  let scopeId = "wpid_test";
  const past: DocGraph[] = [];
  useWorkflowHistoryAccessStore.setState({
    captureImmediately: () => past.push(current),
    undo: () => {
      const previous = past.pop();
      if (previous) current = previous;
    },
  });
  // FlowRenderer's part: a Doc edit marks unsaved changes, and the save data is the live graph serialized.
  const edits: DocGraphEdits = {
    apply: (next) => {
      useWorkflowHasChangesStore.getState().setHasChanges(true);
      current = next;
    },
    update: (change) => {
      useWorkflowHasChangesStore.getState().setHasChanges(true);
      current = { ...current, nodes: change(current.nodes) };
    },
    addStep: () => null,
    duplicate: () => {},
  };
  servedSaveData(() => current);
  const client = new QueryClient();
  // FlowRenderer's part: the page's steps, which outlive the Doc view.
  const Scope = () => {
    useDocImageUploadScope(scopeId, true);
    return null;
  };
  let shown = true;
  const view = () => (
    <QueryClientProvider client={client}>
      <Scope />
      <MemoryRouter>
        <ReactFlowProvider>
          <WorkflowScopeContext.Provider
            value={{ workflowId: "wpid_test", readOnly: false }}
          >
            {shown && <WorkflowDocEditor {...current} edits={edits} />}
          </WorkflowScopeContext.Provider>
        </ReactFlowProvider>
      </MemoryRouter>
    </QueryClientProvider>
  );
  const rendered = render(view());
  rerenderDoc = () => rendered.rerender(view());
  return {
    // A toggle to Visual or YAML unmounts the Doc while the workflow page stays.
    hide: () => {
      shown = false;
      rendered.rerender(view());
    },
    show: () => {
      shown = true;
      rendered.rerender(view());
    },
    data: () =>
      current.nodes.find((n) => n.data.label === "first")?.data as {
        notes?: string | null;
      },
    // What a save sends for the step's screenshots.
    images: () => savedImages(current, "first"),
    // The entries in the graph, reservations included.
    entries: () =>
      (
        current.nodes.find((n) => n.data.label === "first")?.data as {
          docImages?: WorkflowBlockImage[] | null;
        }
      ).docImages,
    rerender: () => rendered.rerender(view()),
    queries: () => client.getQueryCache(),
    snapshots: () => past.length,
    switchWorkflow: () => {
      scopeId = "wpid_other";
      rendered.rerender(view());
    },
    // An edit from outside the Doc, not shown until the next render.
    addImageElsewhere: (fileId: string) => {
      current = {
        ...current,
        nodes: current.nodes.map((n) =>
          n.data.label === "first"
            ? ({
                ...n,
                data: {
                  ...n.data,
                  docImages: [
                    ...((n.data as { docImages?: WorkflowBlockImage[] })
                      .docImages ?? []),
                    image(fileId),
                  ],
                },
              } as typeof n)
            : n,
        ),
      };
    },
    // A typed edit, from the editor's side.
    setNotes: (notes: string) => {
      current = {
        ...current,
        nodes: current.nodes.map((n) =>
          n.data.label === "first"
            ? ({ ...n, data: { ...n.data, notes } } as typeof n)
            : n,
        ),
      };
      rendered.rerender(view());
    },
    deleteStep: () => {
      current = {
        ...current,
        nodes: current.nodes.filter((n) => n.data.label !== "first"),
      };
      rendered.rerender(view());
    },
  };
}

const shot = () => new File(["png"], "shot.png", { type: "image/png" });

function servedSaveData(graph: () => DocGraph) {
  useWorkflowHasChangesStore.setState({
    getSaveData: () =>
      ({
        parameters: [],
        blocks: getWorkflowBlocks(graph().nodes, graph().edges),
        workflowDefinitionVersion: 2,
        title: "Screenshots",
        description: null,
        settings: settings(),
        workflow: { is_saved_task: false, status: "published" },
      }) as never,
  });
}

// A save's blocks are nested by loops, so the step is looked for at any depth.
function savedImages(graph: DocGraph, label: string) {
  type Saved = {
    label: string;
    doc_images?: WorkflowBlockImage[];
    loop_blocks?: Saved[];
  };
  const find = (blocks: Saved[]): Saved | undefined =>
    blocks.reduce<Saved | undefined>(
      (found, b) =>
        found ?? (b.label === label ? b : find(b.loop_blocks ?? [])),
      undefined,
    );
  return find(getWorkflowBlocks(graph.nodes, graph.edges) as Saved[])
    ?.doc_images;
}

function silenceToasts() {
  return vi
    .spyOn(toastModule, "toast")
    .mockReturnValue({ id: "t", dismiss: vi.fn(), update: vi.fn() });
}

// The rendered Doc's rerender: FlowRenderer renders the Doc again after each graph change, the harness on a paste.
let rerenderDoc: (() => void) | null = null;

function pasteInto(line: HTMLElement, data: { text?: string; files?: File[] }) {
  const result = fireEvent.paste(line, {
    clipboardData: {
      getData: (type: string) =>
        type === "text/plain" ? (data.text ?? "") : "",
      files: data.files ?? [],
      items: [],
      types: data.files?.length ? ["Files"] : ["text/plain"],
    },
  });
  act(() => rerenderDoc?.());
  return result;
}

async function openNote() {
  fireEvent.focus(screen.getByRole("textbox", { name: "Note" }));
  const line = screen.getByRole("textbox", { name: "Note" });
  expect(line.getAttribute("contenteditable")).toBe("plaintext-only");
  return line;
}

// A loop and the step after it, edited through the real edit API with an undo and redo stack, as FlowRenderer does.
function renderLoopDoc() {
  let current = getElements(
    [
      forLoop("books", "after", [code("inner", null)]),
      code("after", null, { notes: "Skip books that are sold out" }),
    ],
    settings(),
    true,
  ) as DocGraph;
  const past: DocGraph[] = [];
  const future: DocGraph[] = [];
  const capture = () => {
    past.push(current);
    future.length = 0;
  };
  useWorkflowHistoryAccessStore.setState({
    captureImmediately: capture,
    undo: () => {
      const previous = past.pop();
      if (!previous) return;
      future.push(current);
      current = previous;
    },
    redo: () => {
      const next = future.pop();
      if (!next) return;
      past.push(current);
      current = next;
    },
  });
  servedSaveData(() => current);
  const node = (label: string) =>
    current.nodes.find((n) => n.data.label === label)!;
  const api = () =>
    createDocEditApi({
      ...current,
      edits: {
        apply: (next) => {
          capture();
          current = next;
        },
        update: (change) => {
          current = { ...current, nodes: change(current.nodes) };
        },
        addStep: () => null,
        duplicate: () => {},
      },
      finallyId: null,
      workflowId: "wpid_test",
      selectBranch: () => {},
      selectedBranch: () => undefined,
      inputKeys: [],
      credentialKeys: new Set(),
    });
  const notes = () => ({
    after: { ownerNodeId: node("after").id, placement: "above" as const },
    inner: { ownerNodeId: node("inner").id, placement: "trailing" as const },
  });
  let edit: ReturnType<typeof api> | null = api();
  const slot = { current: null as DocNoteRef | null };
  const hook = renderHook(
    () => {
      useDocImageUploadScope("wpid_test", true);
      return useDocImageFiles(slot.current ?? notes().after);
    },
    {
      wrapper: ({ children }) => (
        <WorkflowDocActionsContext.Provider value={{ edit } as never}>
          {children}
        </WorkflowDocActionsContext.Provider>
      ),
    },
  );
  const noteOf = (n: AppNode, placement: "above" | "trailing") =>
    (n.data as Record<string, unknown>)[
      placement === "above" ? "notes" : "trailingNotes"
    ];
  return {
    notes,
    api: () => {
      edit = api();
      hook.rerender();
      return edit;
    },
    paste: (at: DocNoteRef) => {
      slot.current = at;
      edit = api();
      hook.rerender();
      act(() => hook.result.current.addFiles!([shot()]));
    },
    // Several adds before any render, as two pastes in one tick are: all read the same graph.
    pasteTogether: (at: DocNoteRef, ...adds: File[][]) => {
      slot.current = at;
      edit = api();
      hook.rerender();
      act(() => {
        for (const files of adds) hook.result.current.addFiles!(files);
      });
    },
    lock: () => {
      edit = null;
      hook.rerender();
    },
    saved: (label: string) => savedImages(current, label),
    entries: (label: string) =>
      (node(label).data as { docImages?: unknown }).docImages ?? null,
    text: (label: string, placement: "above" | "trailing") =>
      noteOf(node(label), placement),
    // The note now holding the text that started above "after".
    holder: () => {
      for (const n of current.nodes)
        for (const placement of ["above", "trailing"] as const)
          if (noteOf(n, placement) === "Skip books that are sold out")
            return {
              ownerNodeId: n.id,
              label: n.data.label as string,
              placement,
            };
      throw new Error("the note's text is gone");
    },
  };
}

describe("Doc screenshots", () => {
  test("an image pasted into a note uploads and shows under it; pasted text stays the browser's", async () => {
    post.mockResolvedValue({ data: { file_id: "file_new" } });
    const doc = renderImageDoc({});
    const line = await openNote();

    expect(pasteInto(line, { text: "more words" })).toBe(true);
    expect(post).not.toHaveBeenCalled();

    expect(pasteInto(line, { files: [shot()] })).toBe(false);
    await waitFor(() => expect(doc.images()).toEqual([image("file_new")]));
    expect(post).toHaveBeenCalledWith(
      "/upload_file",
      expect.any(FormData),
      expect.objectContaining({ timeout: 60_000 }),
    );
    doc.rerender();
    expect(
      screen.getByRole("img", { name: "screenshot 1 of 1" }),
    ).toHaveProperty("src", "blob:local-shot");
    expect(get).not.toHaveBeenCalledWith(
      expect.stringContaining("/signed-url"),
      expect.anything(),
    );
  });

  test("a failed upload says so, keeps the note, and Retry adds the image", async () => {
    post.mockRejectedValueOnce(new Error("network"));
    const doc = renderImageDoc({});
    const line = await openNote();
    pasteInto(line, { files: [shot()] });

    expect((await screen.findByRole("alert")).textContent).toMatch(
      /Couldn't upload shot\.png/,
    );
    expect(doc.images()).toBeUndefined();
    expect(doc.data().notes).toBe("Read the plan");
    expect(docAnnouncement()).toMatch(/Couldn't upload shot\.png/);

    post.mockResolvedValueOnce({ data: { file_id: "file_retry" } });
    const retry = screen.getByRole("button", { name: "Retry" });
    retry.focus();
    fireEvent.click(retry);
    await waitFor(() =>
      expect(document.activeElement).toBe(
        screen.getByRole("textbox", { name: "Note" }),
      ),
    );
    await waitFor(() => expect(doc.images()).toEqual([image("file_retry")]));
  });

  test("a file the signed-url route no longer has reads as the route says, even with unsaved edits", async () => {
    get.mockRejectedValue(
      new AxiosError("Not Found", "ERR_BAD_REQUEST", undefined, undefined, {
        status: 404,
      } as never),
    );
    useWorkflowHasChangesStore.setState({ hasChanges: true });
    renderImageDoc({ doc_images: [image("file_gone")] });

    expect(await screen.findByText("File is no longer available")).toBeTruthy();
    expect(get).toHaveBeenCalledWith("/files/file_gone/signed-url", {
      params: { workflow_permanent_id: "wpid_test" },
    });
  });

  test("a local file:// link loads through the artifact server, and a load error is shown, not minted again", async () => {
    const uri = "file:///artifacts/org/my%20shot.png";
    // A second mint would hand back a working https link, so only a refused re-mint leaves the error showing.
    let mints = 0;
    get.mockImplementation(async (url: string) =>
      url.endsWith("/signed-url")
        ? {
            data: {
              file_id: "file_local",
              signed_url: mints++ ? "https://signed/other" : uri,
              expires_at: null,
              content_type: "image/png",
            },
          }
        : { data: {} },
    );
    renderImageDoc({ doc_images: [image("file_local")] });

    const img = await screen.findByRole("img", { name: "screenshot 1 of 1" });
    const src = new URL(img.getAttribute("src")!, "http://doc.test");
    expect(src.pathname).toMatch(/\/artifact\/image$/);
    expect(src.searchParams.get("path")).toBe(uri);

    fireEvent.error(img);
    expect(
      await screen.findByText("Couldn't load this screenshot."),
    ).toBeTruthy();
    expect(screen.queryByRole("img", { name: "screenshot 1 of 1" })).toBeNull();
  });

  test("Remove takes the screenshot away and undo brings it back", async () => {
    get.mockResolvedValue({ data: { signed_url: "https://signed/shot" } });
    const doc = renderImageDoc({ doc_images: [image("file_saved")] });
    await screen.findByRole("img", { name: "screenshot 1 of 1" });

    fireEvent.click(
      screen.getByRole("button", {
        name: "Remove screenshot 1 of 1 from the note above step 1",
      }),
    );
    expect(doc.images()).toBeUndefined();
    doc.rerender();
    expect(screen.queryByRole("img", { name: "screenshot 1 of 1" })).toBeNull();

    act(() => useWorkflowHistoryAccessStore.getState().undo());
    doc.rerender();
    expect(doc.images()).toEqual([image("file_saved")]);
    expect(
      (
        await screen.findByRole("img", { name: "screenshot 1 of 1" })
      ).getAttribute("src"),
    ).toBe("https://signed/shot");
  });

  test("an image over 4 MiB is re-encoded until it fits, or refused; nothing over the limit is uploaded", async () => {
    const LIMIT = 4 * 1024 * 1024;
    vi.stubGlobal(
      "createImageBitmap",
      vi.fn(async () => ({ width: 4000, height: 3000, close() {} })),
    );
    vi.spyOn(HTMLCanvasElement.prototype, "getContext").mockReturnValue({
      drawImage() {},
      fillRect() {},
    } as never);
    // The encoder's output size: over the limit unless `fits` says this try is small enough.
    let fits: (quality: number) => boolean = () => false;
    vi.spyOn(HTMLCanvasElement.prototype, "toBlob").mockImplementation(
      function (callback, type, quality) {
        const size = fits(quality as number) ? 1000 : LIMIT + 1;
        callback(new Blob([new Uint8Array(size)], { type }));
      },
    );
    post.mockResolvedValue({ data: { file_id: "file_small" } });
    const doc = renderImageDoc({});
    const big = () =>
      new File([new Uint8Array(LIMIT + 1)], "big.png", { type: "image/png" });

    pasteInto(await openNote(), { files: [big()] });
    expect((await screen.findByRole("alert")).textContent).toMatch(
      /big\.png is over 4 MiB/,
    );
    expect(post).not.toHaveBeenCalled();

    fits = (quality) => quality < 0.7;
    pasteInto(screen.getByRole("textbox", { name: "Note" }), {
      files: [big()],
    });
    await waitFor(() => expect(doc.images()).toEqual([image("file_small")]));
    const sent = (post.mock.calls[0]![1] as FormData).get("file") as File;
    expect(sent.size).toBeLessThanOrEqual(LIMIT);
    expect([sent.name, sent.type]).toEqual(["big.webp", "image/webp"]);
  });

  test("leaving the Doc mid-upload and coming back keeps the upload: the screenshot arrives", async () => {
    let finish: (value: unknown) => void = () => {};
    post.mockReturnValue(new Promise((resolve) => (finish = resolve)));
    const doc = renderImageDoc({});
    pasteInto(await openNote(), { files: [shot()] });
    await screen.findByText(/Uploading shot\.png/);

    doc.hide();
    finish({ data: { file_id: "file_late" } });
    await waitFor(() => expect(doc.images()).toEqual([image("file_late")]));
    doc.show();
    expect(
      (
        await screen.findByRole("img", { name: "screenshot 1 of 1" })
      ).getAttribute("src"),
    ).toBe("blob:local-shot");
  });

  test("a view toggle before saving keeps an unsaved screenshot's preview", async () => {
    post.mockResolvedValue({ data: { file_id: "file_new" } });
    const doc = renderImageDoc({});
    pasteInto(await openNote(), { files: [shot()] });
    await waitFor(() => expect(doc.images()).toEqual([image("file_new")]));

    doc.hide();
    doc.show();
    expect(
      (
        await screen.findByRole("img", { name: "screenshot 1 of 1" })
      ).getAttribute("src"),
    ).toBe("blob:local-shot");
    expect(URL.revokeObjectURL).not.toHaveBeenCalled();
    expect(screen.queryByText(/shows after the workflow is saved/)).toBeNull();
  });

  test("an uploading screenshot leaves with its step: nothing is added elsewhere, announced or toasted", async () => {
    const notify = silenceToasts();
    let finish: (value: unknown) => void = () => {};
    post.mockReturnValue(new Promise((resolve) => (finish = resolve)));
    const doc = renderImageDoc({});
    pasteInto(await openNote(), { files: [shot()] });
    await screen.findByText(/Uploading shot\.png/);
    expect(doc.entries()!.map((e) => isDocImageHandle(e.file_id))).toEqual([
      true,
    ]);

    doc.deleteStep();
    await act(async () => finish({ data: { file_id: "file_late" } }));
    expect(notify).not.toHaveBeenCalled();
    expect(docAnnouncement()).not.toMatch(/Screenshot added/);
    expect(screen.queryByText(/Uploading shot\.png/)).toBeNull();
    expect(useDocImageUploadStore.getState().uploads).toEqual([]);
  });

  test("a save during an upload goes without it, and the end of the upload marks unsaved changes again", async () => {
    const finish: Array<(value: unknown) => void> = [];
    post.mockImplementation(
      () => new Promise((resolve) => finish.push(resolve)),
    );
    const doc = renderImageDoc({});
    const saved = () =>
      act(() =>
        useWorkflowHasChangesStore.setState((state) => ({
          saveIsPending: false,
          hasChanges: false,
          saveGeneration: state.saveGeneration + 1,
        })),
      );
    pasteInto(await openNote(), { files: [shot()] });
    await waitFor(() => expect(finish).toHaveLength(1));
    expect(useWorkflowHasChangesStore.getState().hasChanges).toBe(true);
    // The save sends the step without the reservation.
    expect(doc.images()).toBeUndefined();
    saved();
    await act(async () => finish[0]!({ data: { file_id: "file_after" } }));
    expect(doc.images()).toEqual([image("file_after")]);
    expect(useWorkflowHasChangesStore.getState().hasChanges).toBe(true);
    expect(docAnnouncement()).toMatch(/^Screenshot added/);

    // Finishing while a save runs: marked once that save is done, and the YAML view is closed.
    saved();
    pasteInto(await openNote(), { files: [shot()] });
    await waitFor(() => expect(finish).toHaveLength(2));
    act(() =>
      useWorkflowHasChangesStore.setState({
        saveIsPending: true,
        hasChanges: false,
      }),
    );
    act(() => useWorkflowYamlEditorStore.setState({ active: true }));
    await act(async () => finish[1]!({ data: { file_id: "file_during" } }));
    // The YAML view, made from the save data, already shows it.
    expect(doc.images()).toEqual([image("file_after"), image("file_during")]);
    saved();
    expect(useWorkflowHasChangesStore.getState().hasChanges).toBe(false);
    act(() => useWorkflowYamlEditorStore.setState({ active: false }));
    await waitFor(() =>
      expect(useWorkflowHasChangesStore.getState().hasChanges).toBe(true),
    );

    // A mark cleared during the upload with no save in between, as a draft back at the saved one clears it: the
    // finished file is still unsaved, so the end of the upload marks it again.
    saved();
    pasteInto(await openNote(), { files: [shot()] });
    await waitFor(() => expect(finish).toHaveLength(3));
    act(() => useWorkflowHasChangesStore.setState({ hasChanges: false }));
    await act(async () => finish[2]!({ data: { file_id: "file_plain" } }));
    expect(useWorkflowHasChangesStore.getState().hasChanges).toBe(true);
  });

  test("an edit undone by hand during an upload can't leave the finished screenshot reading as saved", async () => {
    let finish: (value: unknown) => void = () => {};
    post.mockReturnValue(new Promise((resolve) => (finish = resolve)));
    const doc = renderImageDoc({});
    // The studio's part: a baseline at load, and a dirty recheck after each change to the save data.
    const recheck = () =>
      act(() => useWorkflowSnapshotStore.getState().noteDraftChange(true));
    useWorkflowSnapshotStore.getState().captureSnapshot();
    pasteInto(await openNote(), { files: [shot()] });
    await waitFor(() => expect(post).toHaveBeenCalled());
    doc.setNotes("Read the plan!");
    recheck();
    doc.setNotes("Read the plan");
    recheck();
    // The reservation is no save data, so the draft matches the saved one again and the Doc clears the mark.
    await waitFor(() =>
      expect(useWorkflowHasChangesStore.getState().hasChanges).toBe(false),
    );

    await act(async () => finish({ data: { file_id: "file_late" } }));
    expect(doc.images()).toEqual([image("file_late")]);
    expect(useWorkflowHasChangesStore.getState().hasChanges).toBe(true);
    expect(useWorkflowSnapshotStore.getState().contentDirty).toBe(true);
    doc.rerender();
    expect(screen.getByText("Unsaved changes")).toBeTruthy();
  });

  test("an upload that ends while the YAML view is open marks that view stale, so its commit can't drop the screenshot", async () => {
    let finish: (value: unknown) => void = () => {};
    post.mockReturnValue(new Promise((resolve) => (finish = resolve)));
    renderImageDoc({});
    pasteInto(await openNote(), { files: [shot()] });
    await waitFor(() => expect(post).toHaveBeenCalled());
    act(() => useWorkflowYamlEditorStore.getState().open("title: Screenshots"));
    expect(useWorkflowYamlEditorStore.getState().stale).toBe(false);
    await act(async () => finish({ data: { file_id: "file_late" } }));
    expect(useWorkflowYamlEditorStore.getState().stale).toBe(true);
  });

  test("Dismiss takes the uploading screenshot out as one undo step, keeps the caret on the note, and undo brings it back", async () => {
    let finish: (value: unknown) => void = () => {};
    post.mockReturnValue(new Promise((resolve) => (finish = resolve)));
    const doc = renderImageDoc({});
    const line = await openNote();
    pasteInto(line, { files: [shot()] });
    await screen.findByText(/Uploading shot\.png/);
    expect(doc.snapshots()).toBe(1);

    const dismiss = screen.getByRole("button", { name: "Dismiss" });
    dismiss.focus();
    fireEvent.click(dismiss);
    expect(doc.entries()).toBeNull();
    expect(doc.snapshots()).toBe(2);
    doc.rerender();
    expect(screen.queryByText(/Uploading shot\.png/)).toBeNull();
    await waitFor(() =>
      expect(document.activeElement).toBe(
        screen.getByRole("textbox", { name: "Note" }),
      ),
    );
    await act(async () => finish({ data: { file_id: "file_late" } }));
    expect(doc.images()).toBeUndefined();
    expect(docAnnouncement()).not.toMatch(/Screenshot added/);

    act(() => useWorkflowHistoryAccessStore.getState().undo());
    expect(doc.images()).toEqual([image("file_late")]);
  });

  test("the cap is taken at paste, counting uploads: two files onto nine reserve one and say the cap", async () => {
    const notify = silenceToasts();
    post.mockImplementation(() => new Promise(() => {}));
    get.mockResolvedValue({ data: { signed_url: "https://signed/shot" } });
    const nine = Array.from({ length: 9 }, (_, i) => image(`file_${i}`));
    const doc = renderImageDoc({ doc_images: nine });
    const named = (name: string) =>
      new File(["png"], name, { type: "image/png" });
    pasteInto(await openNote(), { files: [named("a.png"), named("b.png")] });
    expect(doc.entries()).toHaveLength(10);
    expect(doc.snapshots()).toBe(1);
    await waitFor(() =>
      expect(notify).toHaveBeenCalledWith(
        expect.objectContaining({ title: expect.stringMatching(/up to 10/) }),
      ),
    );
    await waitFor(() => expect(post).toHaveBeenCalledTimes(1));
    expect((post.mock.calls[0]![1] as FormData).get("file")).toHaveProperty(
      "name",
      "a.png",
    );
  });

  test("every reason one add is refused for shows in one message", async () => {
    const notify = silenceToasts();
    post.mockImplementation(() => new Promise(() => {}));
    const nine = Array.from({ length: 9 }, (_, i) => image(`file_${i}`));
    renderImageDoc({ doc_images: nine });
    const file = (name: string, type: string) =>
      new File(["x"], name, { type });
    const shown = () =>
      String(notify.mock.calls[notify.mock.calls.length - 1]?.[0]?.title);
    pasteInto(await openNote(), {
      files: [
        file("logo.svg", "image/svg+xml"),
        file("a.png", "image/png"),
        file("b.png", "image/png"),
      ],
    });
    await waitFor(() => expect(shown()).toMatch(/up to 10/));
    expect(shown()).toMatch(/^logo\.svg isn't a PNG/);

    pasteInto(screen.getByRole("textbox", { name: "Note" }), {
      files: [
        file("one.svg", "image/svg+xml"),
        file("two.txt", "text/plain"),
        file("three.bmp", "image/bmp"),
      ],
    });
    expect(shown()).toMatch(/one\.svg.*two\.txt.*three\.bmp/);
  });

  test("a screenshot still uploading holds its place under the cap: the next paste is refused", async () => {
    const notify = silenceToasts();
    post.mockReturnValue(new Promise(() => {}));
    get.mockResolvedValue({ data: { signed_url: "https://signed/shot" } });
    const nine = Array.from({ length: 9 }, (_, i) => image(`file_${i}`));
    const doc = renderImageDoc({ doc_images: nine });
    pasteInto(await openNote(), { files: [shot()] });
    await screen.findByText(/Uploading shot\.png/);

    pasteInto(screen.getByRole("textbox", { name: "Note" }), {
      files: [shot()],
    });
    expect(doc.entries()).toHaveLength(10);
    expect(doc.snapshots()).toBe(1);
    expect(post).toHaveBeenCalledTimes(1);
    expect(notify).toHaveBeenCalledWith(
      expect.objectContaining({ title: expect.stringMatching(/up to 10/) }),
    );
  });

  test("two pastes before a render can't pass the cap: the second keeps no rows and says the cap", async () => {
    const notify = silenceToasts();
    post.mockReturnValue(new Promise(() => {}));
    const doc = renderLoopDoc();
    const six = () => Array.from({ length: 6 }, () => shot());
    doc.pasteTogether(doc.notes().after, six(), six());
    await waitFor(() => expect(post).toHaveBeenCalled());
    expect(doc.entries("after")).toHaveLength(6);
    expect(useDocImageUploadStore.getState().uploads).toHaveLength(6);
    expect(notify).toHaveBeenCalledWith(
      expect.objectContaining({ title: expect.stringMatching(/up to 10/) }),
    );
  });

  test("with the images flag off nothing adds a screenshot, while saved ones still show", async () => {
    imagesFlag.on = false;
    get.mockResolvedValue({ data: { signed_url: "https://signed/saved" } });
    renderImageDoc({ doc_images: [image("file_saved")] });
    const line = await openNote();

    expect(
      screen.queryByRole("button", { name: /Add a screenshot/ }),
    ).toBeNull();
    expect(pasteInto(line, { files: [shot()] })).toBe(true);
    expect(
      fireEvent.drop(line, {
        dataTransfer: { files: [shot()], types: ["Files"] },
      }),
    ).toBe(true);
    expect(post).not.toHaveBeenCalled();
    expect(
      (
        await screen.findByRole("img", { name: "screenshot 1 of 1" })
      ).getAttribute("src"),
    ).toBe("https://signed/saved");
    expect(screen.getByRole("button", { name: /Remove/ })).toBeTruthy();

    imagesFlag.on = true;
    cleanup();
    renderImageDoc({});
    expect(
      screen.getByRole("button", { name: /Add a screenshot/ }),
    ).toBeTruthy();
  });

  test("another workflow, or leaving the editor, releases the uploads and previews", () => {
    const scope = renderHook(
      ({ id, hosts }) => useDocImageUploadScope(id, hosts),
      { initialProps: { id: "wpid_a", hosts: true } },
    );
    rememberLocalImage("file_x", new Blob(["png"]));
    scope.rerender({ id: "wpid_a", hosts: true });
    expect(localImageUrl("file_x")).toBe("blob:local-shot");

    scope.rerender({ id: "wpid_b", hosts: true });
    expect(localImageUrl("file_x")).toBeUndefined();
    expect(URL.revokeObjectURL).toHaveBeenCalledWith("blob:local-shot");

    rememberLocalImage("file_y", new Blob(["png"]));
    scope.unmount();
    expect(localImageUrl("file_y")).toBeUndefined();
  });

  test("a file copied in the file manager pastes as the image, while a cell with a picture stays text", async () => {
    post.mockResolvedValue({ data: { file_id: "file_copied" } });
    const doc = renderImageDoc({});
    const line = await openNote();

    expect(pasteInto(line, { text: "Q1 total", files: [shot()] })).toBe(true);
    expect(post).not.toHaveBeenCalled();

    expect(pasteInto(line, { text: "shot.png", files: [shot()] })).toBe(false);
    await waitFor(() => expect(doc.images()).toEqual([image("file_copied")]));
  });

  describe("over 4 MiB", () => {
    const LIMIT = 4 * 1024 * 1024;
    // A WebP header whose VP8X chunk has the animation flag (0x02) set or not.
    const webp = (animated: boolean, size: number) => {
      const bytes = new Uint8Array(size);
      bytes.set(new TextEncoder().encode("RIFF"), 0);
      bytes.set(new TextEncoder().encode("WEBPVP8X"), 8);
      bytes[20] = animated ? 0x02 : 0x00;
      return new File([bytes], "clip.webp", { type: "image/webp" });
    };
    beforeEach(() => {
      vi.stubGlobal(
        "createImageBitmap",
        vi.fn(async () => ({ width: 400, height: 300, close() {} })),
      );
      vi.spyOn(HTMLCanvasElement.prototype, "getContext").mockReturnValue({
        drawImage() {},
        fillRect() {},
      } as never);
      vi.spyOn(HTMLCanvasElement.prototype, "toBlob").mockImplementation(
        function (callback, type) {
          callback(new Blob([new Uint8Array(1000)], { type }));
        },
      );
      post.mockResolvedValue({ data: { file_id: "file_fit" } });
    });
    const ANIMATED =
      /animated image is over 4 MiB and cannot be shrunk without losing the animation/;

    test("large files added together are decoded one at a time", async () => {
      const decoded: Array<() => void> = [];
      vi.stubGlobal(
        "createImageBitmap",
        vi.fn(
          () =>
            new Promise((resolve) =>
              decoded.push(() =>
                resolve({ width: 400, height: 300, close() {} }),
              ),
            ),
        ),
      );
      const doc = renderImageDoc({});
      const big = (name: string) =>
        new File([new Uint8Array(LIMIT + 1)], name, { type: "image/png" });
      pasteInto(await openNote(), { files: [big("a.png"), big("b.png")] });
      await waitFor(() => expect(decoded).toHaveLength(1));
      await act(async () => decoded[0]!());
      await waitFor(() => expect(decoded).toHaveLength(2));
      await act(async () => decoded[1]!());
      await waitFor(() => expect(doc.images()).toHaveLength(2));
    });

    test("a GIF is refused as one that may be animated, not turned into a still", async () => {
      renderImageDoc({});
      const gif = new File([new Uint8Array(LIMIT + 1)], "clip.gif", {
        type: "image/gif",
      });
      pasteInto(await openNote(), { files: [gif] });
      expect((await screen.findByRole("alert")).textContent).toMatch(
        /GIF may be animated and is over 4 MiB/,
      );
      expect(post).not.toHaveBeenCalled();
    });

    // A PNG whose chunks after IHDR are acTL (animated) or straight IDAT (still).
    const png = (animated: boolean, size: number) => {
      const bytes = new Uint8Array(size);
      const view = new DataView(bytes.buffer);
      const put = (at: number, text: string) =>
        bytes.set(new TextEncoder().encode(text), at);
      bytes.set([0x89, 0x50, 0x4e, 0x47, 0x0d, 0x0a, 0x1a, 0x0a], 0);
      view.setUint32(8, 13);
      put(12, "IHDR");
      view.setUint32(33, 8);
      put(37, animated ? "acTL" : "IDAT");
      return new File([bytes], "clip.png", { type: "image/png" });
    };

    test("an animated PNG is refused and a still PNG is re-encoded", async () => {
      const doc = renderImageDoc({});
      pasteInto(await openNote(), { files: [png(true, LIMIT + 1)] });
      expect((await screen.findByRole("alert")).textContent).toMatch(ANIMATED);
      expect(post).not.toHaveBeenCalled();

      pasteInto(screen.getByRole("textbox", { name: "Note" }), {
        files: [png(false, LIMIT + 1)],
      });
      await waitFor(() => expect(doc.images()).toEqual([image("file_fit")]));
    });

    test("an animated WebP is refused and a still WebP is re-encoded", async () => {
      const doc = renderImageDoc({});
      pasteInto(await openNote(), { files: [webp(true, LIMIT + 1)] });
      expect((await screen.findByRole("alert")).textContent).toMatch(ANIMATED);
      expect(post).not.toHaveBeenCalled();

      pasteInto(screen.getByRole("textbox", { name: "Note" }), {
        files: [webp(false, LIMIT + 1)],
      });
      await waitFor(() => expect(doc.images()).toEqual([image("file_fit")]));
      const sent = (post.mock.calls[0]![1] as FormData).get("file") as File;
      expect(sent.size).toBe(1000);
    });

    test("an animated image at the limit is uploaded untouched", async () => {
      const doc = renderImageDoc({});
      const gif = new File([new Uint8Array(LIMIT)], "clip.gif", {
        type: "image/gif",
      });
      pasteInto(await openNote(), { files: [gif, webp(true, LIMIT)] });
      await waitFor(() => expect(post).toHaveBeenCalledTimes(2));
      const sent = post.mock.calls.map(
        (call) => (call[1] as FormData).get("file") as File,
      );
      expect(sent.map((f) => [f.name, f.size])).toEqual([
        ["clip.gif", LIMIT],
        ["clip.webp", LIMIT],
      ]);
      expect(createImageBitmap).not.toHaveBeenCalled();
      await waitFor(() => expect(doc.images()).toHaveLength(2));
    });

    test("an image that can't be decoded says so, not that it is too large", async () => {
      vi.stubGlobal(
        "createImageBitmap",
        vi.fn(async () => {
          throw new Error("decode");
        }),
      );
      renderImageDoc({});
      const broken = new File([new Uint8Array(LIMIT + 1)], "bad.png", {
        type: "image/png",
      });
      pasteInto(await openNote(), { files: [broken] });
      expect((await screen.findByRole("alert")).textContent).toMatch(
        /bad\.png couldn't be read as an image/,
      );
    });
  });

  test("a note that holds only a screenshot still shows it", async () => {
    get.mockResolvedValue({ data: { signed_url: "https://signed/shot" } });
    renderImageDoc({ notes: null, doc_images: [image("file_only")] });
    expect(
      (
        await screen.findByRole("img", { name: "screenshot 1 of 1" })
      ).getAttribute("src"),
    ).toBe("https://signed/shot");
  });

  test("a screenshot after a step shows under the trailing note", async () => {
    get.mockResolvedValue({ data: { signed_url: "https://signed/after" } });
    renderImageDoc({
      notes: null,
      trailing_notes: "Then check the total",
      doc_images: [image("file_after", "trailing")],
    });
    expect(
      await screen.findByRole("img", { name: "screenshot 1 of 1" }),
    ).toBeTruthy();
    expect(
      screen.getByRole("button", {
        name: "Remove screenshot 1 of 1 from the note after step 1",
      }),
    ).toBeTruthy();
  });

  test("a note Tabbed into an empty loop keeps its screenshots and upload rows there; Remove and undo work in place", async () => {
    post.mockReturnValue(new Promise(() => {}));
    get.mockResolvedValue({ data: { signed_url: "https://signed/shot" } });
    const doc = renderImageDoc(
      {
        notes: null,
        trailing_notes: "Open each book page",
        doc_images: [image("file_saved", "trailing")],
      },
      (label, data) => forLoop(label, null, [], data),
    );
    await screen.findByRole("img", { name: "screenshot 1 of 1" });
    pasteInto(await openNote(), { files: [shot()] });
    doc.rerender();
    await screen.findByText(/Uploading shot\.png/);

    fireEvent.keyDown(screen.getByRole("textbox", { name: "Note" }), {
      key: "Tab",
    });
    await act(() => new Promise((r) => requestAnimationFrame(r)));
    const nested = screen.getByRole("textbox", { name: "Note" });
    expect(nested.closest(".border-l")).toBeTruthy();
    const tile = screen.getByRole("img", { name: "screenshot 1 of 1" });
    expect(tile.closest(".border-l")).toBeTruthy();
    expect(
      screen.getByText(/Uploading shot\.png/).closest(".border-l"),
    ).toBeTruthy();

    fireEvent.click(
      screen.getByRole("button", { name: /^Remove screenshot 1 of 1/ }),
    );
    doc.rerender();
    expect(screen.queryByRole("img", { name: "screenshot 1 of 1" })).toBeNull();
    act(() => useWorkflowHistoryAccessStore.getState().undo());
    doc.rerender();
    expect(
      (await screen.findByRole("img", { name: "screenshot 1 of 1" })).closest(
        ".border-l",
      ),
    ).toBeTruthy();
    expect(
      (doc.data() as { trailingNotes?: string | null }).trailingNotes,
    ).toBe("Open each book page");
  });

  test("files dropped on a note, or picked, upload into it; the picker can pick the same file again", async () => {
    post
      .mockResolvedValueOnce({ data: { file_id: "file_dropped" } })
      .mockResolvedValueOnce({ data: { file_id: "file_picked" } });
    const doc = renderImageDoc({});
    const line = await openNote();
    fireEvent.drop(line, {
      dataTransfer: { files: [shot()], types: ["Files"] },
    });
    await waitFor(() => expect(doc.images()).toEqual([image("file_dropped")]));
    doc.rerender();

    const picker = screen.getByRole("button", {
      name: "Add a screenshot to the note above step 1",
    });
    expect(picker.getAttribute("title")).toMatch(/the agent does not see it/);
    const input = document.querySelector<HTMLInputElement>(
      "[data-doc-image-input]",
    )!;
    // A browser fires no change for the same file twice unless the input is cleared.
    const cleared: string[] = [];
    Object.defineProperty(input, "value", {
      configurable: true,
      get: () => "",
      set: (value: string) => cleared.push(value),
    });
    fireEvent.change(input, { target: { files: [shot()] } });
    await waitFor(() =>
      expect(doc.images()).toEqual([
        image("file_dropped"),
        image("file_picked"),
      ]),
    );
    expect(cleared).toEqual([""]);
  });

  test("files added together are reserved in the order they were picked, whatever finishes first; a failed one holds none back", async () => {
    const finish = new Map<string, (value: unknown) => void>();
    post.mockImplementation((_url: string, form: FormData) => {
      const name = (form.get("file") as File).name;
      if (name === "b.png") return Promise.reject(new Error("network"));
      return new Promise((resolve) => finish.set(name, resolve));
    });
    const doc = renderImageDoc({});
    const named = (name: string) =>
      new File(["png"], name, { type: "image/png" });
    pasteInto(await openNote(), {
      files: [named("a.png"), named("b.png"), named("c.png")],
    });
    await waitFor(() => expect(finish.size).toBe(2));
    await act(async () =>
      finish.get("c.png")!({ data: { file_id: "file_c" } }),
    );
    expect(doc.images()).toEqual([image("file_c")]);
    await act(async () =>
      finish.get("a.png")!({ data: { file_id: "file_a" } }),
    );
    await waitFor(() =>
      expect(doc.images()).toEqual([image("file_a"), image("file_c")]),
    );
    doc.rerender();
    expect((await screen.findByRole("alert")).textContent).toMatch(
      /Couldn't upload b\.png/,
    );
  });

  test("Tab, Shift+Tab, undo and redo move an uploading screenshot with its text; it lands under that text", async () => {
    const finish: Array<(value: unknown) => void> = [];
    post.mockImplementation(
      () => new Promise((resolve) => finish.push(resolve)),
    );
    const doc = renderLoopDoc();
    const { after, inner } = doc.notes();

    // Tab a note into the loop, then paste on it there.
    expect(doc.api().nestNote(after, false)).toEqual(inner);
    doc.paste(inner);
    await waitFor(() => expect(finish).toHaveLength(1));
    expect(doc.saved("inner")).toBeUndefined();

    // Shift+Tab takes the uploading screenshot back out with the text.
    expect(doc.api().nestNote(inner, true)).not.toBeNull();
    const out = doc.holder();
    expect(out.ownerNodeId).not.toBe(inner.ownerNodeId);
    await act(async () => finish[0]!({ data: { file_id: "file_moved" } }));
    expect(doc.saved(out.label)).toEqual([image("file_moved", out.placement)]);
    expect(doc.text(out.label, out.placement)).toBe(
      "Skip books that are sold out",
    );
    expect(docAnnouncement()).toMatch(/^Screenshot added/);
  });

  test("Turn into plain text carries an uploading screenshot with the step's text; it lands under that text", async () => {
    let finish: (value: unknown) => void = () => {};
    post.mockReturnValue(new Promise((resolve) => (finish = resolve)));
    const doc = renderLoopDoc();
    const { after } = doc.notes();
    doc.paste({ ownerNodeId: after.ownerNodeId, placement: "block" } as never);
    await waitFor(() => expect(post).toHaveBeenCalled());

    expect(doc.api().toPlainText(after.ownerNodeId)).not.toBeNull();
    const out = doc.holder();
    expect(out.label).not.toBe("after");
    await act(async () => finish({ data: { file_id: "file_turned" } }));
    expect(doc.saved(out.label)).toEqual([image("file_turned", out.placement)]);
  });

  test("undo during an upload takes the paste away, the late upload announces nothing, and redo puts it back under its text", async () => {
    let finish: (value: unknown) => void = () => {};
    post.mockReturnValue(new Promise((resolve) => (finish = resolve)));
    const doc = renderLoopDoc();
    const { after, inner } = doc.notes();
    doc.api().nestNote(after, false);
    doc.paste(inner);
    await waitFor(() => expect(post).toHaveBeenCalled());

    act(() => useWorkflowHistoryAccessStore.getState().undo());
    expect(doc.text("inner", "trailing")).toBe("Skip books that are sold out");
    expect(doc.entries("inner")).toBeNull();
    await act(async () => finish({ data: { file_id: "file_late" } }));
    expect(doc.saved("inner")).toBeUndefined();
    expect(doc.saved("after")).toBeUndefined();
    expect(docAnnouncement()).not.toMatch(/Screenshot added/);

    // A second undo puts the text back above its step; no screenshot comes with it.
    act(() => useWorkflowHistoryAccessStore.getState().undo());
    expect(doc.text("after", "above")).toBe("Skip books that are sold out");
    expect(doc.saved("after")).toBeUndefined();

    act(() => useWorkflowHistoryAccessStore.getState().redo());
    act(() => useWorkflowHistoryAccessStore.getState().redo());
    expect(doc.text("inner", "trailing")).toBe("Skip books that are sold out");
    expect(doc.saved("inner")).toEqual([image("file_late", "trailing")]);
  });

  test.each([
    ["a Copilot acceptance"],
    ["a pending Copilot proposal"],
    ["a demonstration"],
  ])(
    "an upload that finishes while %s locks the Doc still lands, with no toast",
    async () => {
      // Each lock leaves the Doc with no edit API; the upload's end needs none.
      const notify = silenceToasts();
      let finish: (value: unknown) => void = () => {};
      post.mockReturnValue(new Promise((resolve) => (finish = resolve)));
      const doc = renderLoopDoc();
      const { after } = doc.notes();
      doc.paste(after);
      await waitFor(() => expect(post).toHaveBeenCalled());
      doc.lock();
      await act(async () => finish({ data: { file_id: "file_locked" } }));
      expect(doc.saved("after")).toEqual([image("file_locked")]);
      expect(useDocImageUploadStore.getState().uploads).toEqual([]);
      expect(notify).not.toHaveBeenCalled();
      expect(docAnnouncement()).toMatch(/^Screenshot added/);
    },
  );

  test("an image pasted into a step's text goes under the text and says so", async () => {
    const notify = vi
      .spyOn(toastModule, "toast")
      .mockReturnValue({ id: "t", dismiss: vi.fn(), update: vi.fn() });
    post.mockResolvedValue({ data: { file_id: "file_block" } });
    const doc = renderImageDoc({}, (label, data) =>
      block("text_prompt", label, null, { prompt: "Summarize it", ...data }),
    );
    fireEvent.focus(screen.getByRole("textbox", { name: "first prompt" }));
    pasteInto(screen.getByRole("textbox", { name: "first prompt" }), {
      files: [shot()],
    });
    await waitFor(() =>
      expect(doc.images()).toEqual([image("file_block", "block")]),
    );
    expect(notify).toHaveBeenCalledWith(
      expect.objectContaining({
        title: "Screenshot added below the step's text",
      }),
    );
  });

  test("undo after adding a screenshot takes it away", async () => {
    post.mockResolvedValue({ data: { file_id: "file_new" } });
    const doc = renderImageDoc({});
    pasteInto(await openNote(), { files: [shot()] });
    await waitFor(() => expect(doc.images()).toEqual([image("file_new")]));
    act(() => useWorkflowHistoryAccessStore.getState().undo());
    expect(doc.images()).toBeUndefined();
  });

  test("an upload that finishes after the page moved to another workflow adds nothing", async () => {
    let finish: (value: unknown) => void = () => {};
    post.mockReturnValue(new Promise((resolve) => (finish = resolve)));
    const doc = renderImageDoc({});
    pasteInto(await openNote(), { files: [shot()] });
    await screen.findByText(/Uploading shot\.png/);
    doc.switchWorkflow();
    await act(async () => finish({ data: { file_id: "file_late" } }));
    expect(doc.images()).toBeUndefined();
    expect(docAnnouncement()).not.toMatch(/Screenshot added/);
  });

  test.each([
    ["an SVG", "image/svg+xml", "a.svg"],
    ["a HEIC", "image/heic", "a.heic"],
    ["an AVIF", "image/avif", "a.avif"],
    ["a BMP", "image/bmp", "a.bmp"],
    ["a file with no type", "", "a"],
  ])("%s is refused before any upload", async (_name, type, name) => {
    const notify = silenceToasts();
    const doc = renderImageDoc({});
    pasteInto(await openNote(), {
      files: [new File(["x"], name, { type })],
    });
    expect(notify).toHaveBeenCalledWith(
      expect.objectContaining({
        title: expect.stringMatching(/isn't a PNG, JPEG, GIF or WebP image/),
      }),
    );
    expect(docAnnouncement()).toMatch(/isn't a PNG, JPEG, GIF or WebP image/);
    expect(doc.entries()).toBeUndefined();
    expect(post).not.toHaveBeenCalled();
  });

  test("the 30 MiB ceiling: at it the file is taken, past it refused", () => {
    const sized = (size: number) =>
      new File([new Uint8Array(size)], "big.png", { type: "image/png" });
    expect(docImageRefusal(sized(31457280))).toBeNull();
    expect(docImageRefusal(sized(31457281))).toMatch(/over the 30 MiB limit/);
  });

  test("two screenshots under a note have their own names, and Remove puts the caret back on that note, not the first line", async () => {
    get.mockImplementation(async (url: string) => ({
      data: { signed_url: `https://signed${url}`, expires_at: 1 },
    }));
    renderImageDoc({
      notes: null,
      trailing_notes: "Then check the total",
      doc_images: [image("file_a", "trailing"), image("file_b", "trailing")],
    });
    expect(
      await screen.findByRole("img", { name: "screenshot 2 of 2" }),
    ).toBeTruthy();
    fireEvent.click(
      screen.getByRole("button", {
        name: "Remove screenshot 2 of 2 from the note after step 1",
      }),
    );
    await waitFor(() =>
      expect(document.activeElement).toBe(
        screen.getByRole("textbox", { name: "Note" }),
      ),
    );
  });

  test("an expired https link is minted once more on a load error; a second error in a row is shown", async () => {
    let mints = 0;
    get.mockImplementation(async (url: string) =>
      url.endsWith("/signed-url")
        ? { data: { signed_url: `https://signed/${++mints}`, expires_at: 1 } }
        : { data: {} },
    );
    renderImageDoc({ doc_images: [image("file_s")] });
    const first = await screen.findByRole("img", { name: "screenshot 1 of 1" });
    expect(first.getAttribute("src")).toBe("https://signed/1");

    fireEvent.error(first);
    await waitFor(() =>
      expect(
        screen
          .getByRole("img", { name: "screenshot 1 of 1" })
          .getAttribute("src"),
      ).toBe("https://signed/2"),
    );
    // A load in between resets the one-retry budget.
    fireEvent.load(screen.getByRole("img", { name: "screenshot 1 of 1" }));
    fireEvent.error(screen.getByRole("img", { name: "screenshot 1 of 1" }));
    await waitFor(() =>
      expect(
        screen
          .getByRole("img", { name: "screenshot 1 of 1" })
          .getAttribute("src"),
      ).toBe("https://signed/3"),
    );
    fireEvent.error(screen.getByRole("img", { name: "screenshot 1 of 1" }));
    expect(
      await screen.findByText("Couldn't load this screenshot."),
    ).toBeTruthy();
  });

  test("a re-mint that hands back the same link is shown as an error, not looped", async () => {
    get.mockResolvedValue({
      data: { signed_url: "https://signed/same", expires_at: 1 },
    });
    renderImageDoc({ doc_images: [image("file_s")] });
    fireEvent.error(
      await screen.findByRole("img", { name: "screenshot 1 of 1" }),
    );
    expect(
      await screen.findByText("Couldn't load this screenshot."),
    ).toBeTruthy();
  });

  test("a screenshot shown as broken loads again when a new link comes", async () => {
    get.mockResolvedValue({
      data: { signed_url: "https://signed/same", expires_at: 1 },
    });
    const doc = renderImageDoc({ doc_images: [image("file_s")] });
    fireEvent.error(
      await screen.findByRole("img", { name: "screenshot 1 of 1" }),
    );
    expect(
      await screen.findByText("Couldn't load this screenshot."),
    ).toBeTruthy();

    get.mockResolvedValue({
      data: { signed_url: "https://signed/fresh", expires_at: 1 },
    });
    await act(() =>
      doc
        .queries()
        .find({ queryKey: ["docImageSignedUrl"], exact: false })!
        .fetch(),
    );
    expect(
      (
        await screen.findByRole("img", { name: "screenshot 1 of 1" })
      ).getAttribute("src"),
    ).toBe("https://signed/fresh");
  });

  test("a signed link is asked for again after 50 minutes on focus, a link with no expiry never", async () => {
    vi.useFakeTimers({ toFake: ["Date"] });
    try {
      const mints: string[] = [];
      get.mockImplementation(async (url: string) => {
        if (!url.endsWith("/signed-url")) return { data: {} };
        mints.push(url);
        return {
          data: {
            signed_url: `https://signed${url}`,
            expires_at: url.includes("file_s") ? 1 : null,
          },
        };
      });
      renderImageDoc({ doc_images: [image("file_s"), image("file_l")] });
      await screen.findByRole("img", { name: "screenshot 2 of 2" });
      const refocus = () =>
        act(() => {
          focusManager.setFocused(false);
          focusManager.setFocused(true);
        });
      vi.setSystemTime(Date.now() + 49 * 60 * 1000);
      refocus();
      expect(mints).toHaveLength(2);
      vi.setSystemTime(Date.now() + 2 * 60 * 1000);
      refocus();
      await waitFor(() => expect(mints).toHaveLength(3));
      expect(mints[2]).toContain("file_s");
    } finally {
      focusManager.setFocused(undefined);
      vi.useRealTimers();
    }
  });

  test("a screenshot added here reads as waiting for the save, then loads once saved", async () => {
    post.mockResolvedValue({ data: { file_id: "file_new" } });
    const notFound = new AxiosError(
      "Not Found",
      "ERR_BAD_REQUEST",
      undefined,
      undefined,
      {
        status: 404,
      } as never,
    );
    get.mockRejectedValue(notFound);
    const doc = renderImageDoc({});
    pasteInto(await openNote(), { files: [shot()] });
    await waitFor(() => expect(doc.images()).toEqual([image("file_new")]));
    // The preview is gone (another tab's editor released it); the saved definition doesn't name the file yet.
    releaseDocImageUploads();
    useWorkflowHasChangesStore.setState({ hasChanges: true });
    doc.rerender();
    expect(
      await screen.findByText(
        "This screenshot shows after the workflow is saved.",
      ),
    ).toBeTruthy();

    get.mockResolvedValue({
      data: { signed_url: "https://signed/saved", expires_at: 1 },
    });
    act(() => useWorkflowHasChangesStore.setState({ hasChanges: false }));
    expect(
      (
        await screen.findByRole("img", { name: "screenshot 1 of 1" })
      ).getAttribute("src"),
    ).toBe("https://signed/saved");
  });

  test("a step at its screenshot limit refuses another without uploading", async () => {
    get.mockResolvedValue({ data: { signed_url: "https://signed/shot" } });
    const full = Array.from({ length: 10 }, (_, i) => image(`file_${i}`));
    const notify = silenceToasts();
    const doc = renderImageDoc({ doc_images: full });
    const line = await openNote();
    pasteInto(line, { files: [shot()] });

    expect(notify).toHaveBeenCalledWith(
      expect.objectContaining({ title: expect.stringMatching(/up to 10/) }),
    );
    expect(doc.snapshots()).toBe(0);
    expect(post).not.toHaveBeenCalled();
    expect(doc.images()).toHaveLength(10);
  });
});

describe("fitDocImage", () => {
  const big = (type = "image/png") =>
    new File([new Uint8Array(4194305)], "big.png", { type });
  let close: ReturnType<typeof vi.fn>;
  let context: { fillStyle: string; fills: string[] };
  // Each toBlob call: the canvas width, the asked type and quality; `answer` decides what comes back.
  let tries: Array<[number, string, number]>;
  let answer: (width: number, type: string, quality: number) => Blob | null;

  beforeEach(() => {
    close = vi.fn();
    tries = [];
    vi.stubGlobal(
      "createImageBitmap",
      vi.fn(async () => ({ width: 4000, height: 3000, close })),
    );
    context = { fillStyle: "", fills: [] };
    vi.spyOn(HTMLCanvasElement.prototype, "getContext").mockReturnValue({
      drawImage() {},
      set fillStyle(value: string) {
        context.fillStyle = value;
      },
      get fillStyle() {
        return context.fillStyle;
      },
      globalCompositeOperation: "",
      fillRect() {
        context.fills.push(context.fillStyle);
      },
    } as never);
    vi.spyOn(HTMLCanvasElement.prototype, "toBlob").mockImplementation(
      function (this: HTMLCanvasElement, callback, type, quality) {
        tries.push([this.width, type!, quality as number]);
        callback(answer(this.width, type!, quality as number));
      },
    );
  });

  const sized = (size: number, type: string) =>
    new Blob([new Uint8Array(size)], { type });

  test("stops and refuses when nothing fits", async () => {
    answer = (_w, type) => sized(4194305, type);
    expect(await fitDocImage(big())).toMatch(/over 4 MiB/);
    expect(tries.length).toBeGreaterThan(1);
    expect(close).toHaveBeenCalled();
  });

  test("a result of exactly 4 MiB is taken; a file of exactly 4 MiB is not re-encoded", async () => {
    answer = (_w, type) => sized(4194304, type);
    const fitted = (await fitDocImage(big())) as File;
    expect([fitted.size, fitted.type]).toEqual([4194304, "image/webp"]);
    const exact = new File([new Uint8Array(4194304)], "exact.png", {
      type: "image/png",
    });
    tries = [];
    expect(await fitDocImage(exact)).toBe(exact);
    expect(tries).toEqual([]);
  });

  test("a page taller than 16383 px starts at a scale whose longest side fits, so the encode isn't cropped", async () => {
    expect(docImageScales(4000, 3000)).toEqual([1, 0.75, 0.5, 0.35]);
    expect(Math.round(24000 * docImageScales(2880, 24000)[0]!)).toBe(16383);
    vi.stubGlobal(
      "createImageBitmap",
      vi.fn(async () => ({ width: 2880, height: 24000, close })),
    );
    answer = (_w, type) => sized(10, type);
    await fitDocImage(big());
    expect(tries[0]![0]).toBe(Math.round((2880 * 16383) / 24000));
  });

  test("an image over 80 megapixels is refused without encoding it", async () => {
    vi.stubGlobal(
      "createImageBitmap",
      vi.fn(async () => ({ width: 10000, height: 8001, close })),
    );
    answer = (_w, type) => sized(10, type);
    expect(await fitDocImage(big())).toMatch(/over 80 megapixels/);
    expect(tries).toEqual([]);
    expect(close).toHaveBeenCalled();
  });

  test.each([
    [
      "a PNG",
      "image/png",
      (b: Uint8Array, v: DataView) => {
        b.set(new TextEncoder().encode("IHDR"), 12);
        v.setUint32(16, 10000);
        v.setUint32(20, 8001);
      },
    ],
    [
      "a WebP",
      "image/webp",
      (b: Uint8Array) => {
        b.set(new TextEncoder().encode("RIFF"), 0);
        b.set(new TextEncoder().encode("WEBPVP8X"), 8);
        // Width and height minus one, 24-bit little-endian: 9999 and 8000.
        b.set([0x0f, 0x27, 0x00, 0x40, 0x1f, 0x00], 24);
      },
    ],
    [
      "a JPEG behind an APP1 segment",
      "image/jpeg",
      (b: Uint8Array, v: DataView) => {
        b.set([0xff, 0xd8, 0xff, 0xe1], 0);
        v.setUint16(4, 100);
        b.set([0xff, 0xc0], 104);
        v.setUint16(106, 17);
        v.setUint16(109, 8001);
        v.setUint16(111, 10000);
      },
    ],
  ])(
    "%s whose header says over 80 megapixels is refused before it is decoded",
    async (_name, type, write) => {
      const bytes = new Uint8Array(4194305);
      write(bytes, new DataView(bytes.buffer));
      const decode = vi.fn();
      vi.stubGlobal("createImageBitmap", decode);
      const file = new File([bytes], "huge", { type });
      expect(await fitDocImage(file)).toMatch(/over 80 megapixels/);
      expect(decode).not.toHaveBeenCalled();
    },
  );

  test("a browser without a WebP encoder gets a JPEG on a white backing", async () => {
    answer = (_w, type) =>
      type === "image/webp" ? sized(10, "image/png") : sized(10, type);
    const fitted = (await fitDocImage(big())) as File;
    expect([fitted.name, fitted.type]).toEqual(["big.jpg", "image/jpeg"]);
    expect(context.fills).toEqual(["#fff"]);
  });
});
