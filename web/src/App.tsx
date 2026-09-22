import { useCallback, useEffect, useState } from "react";
import { api } from "@/api";
import type { DatasetInfo, WorkspaceInfo } from "@/types";
import { Notice, Spinner } from "@/components/ui/misc";
import Landing, { type Preview } from "./components/Landing";
import Workbench from "./components/Workbench";

export default function App() {
  const [workspace, setWorkspace] = useState<WorkspaceInfo | null>(null);
  const [wsError, setWsError] = useState<string | null>(null);
  const [dataset, setDataset] = useState<DatasetInfo | null>(null);
  const [note, setNote] = useState<string | undefined>(undefined);
  const [initialPrompt, setInitialPrompt] = useState<string | undefined>(undefined);
  const [preview, setPreview] = useState<Preview | null>(null);

  const refresh = useCallback(() => {
    api.workspace().then(setWorkspace).catch((e) => setWsError((e as Error).message));
  }, []);

  useEffect(refresh, [refresh]);

  // Deep link: #/d/<dataset_id>
  useEffect(() => {
    const m = location.hash.match(/^#\/d\/([\w-]+)/);
    if (m && !dataset) api.dataset(m[1]).then(setDataset).catch(() => history.replaceState(null, "", "#"));
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  useEffect(() => {
    history.replaceState(null, "", dataset ? `#/d/${dataset.dataset_id}` : "#");
  }, [dataset]);

  if (wsError) {
    return (
      <div className="mx-auto max-w-2xl px-6 pt-20">
        <span className="font-mono text-[11px] tracking-[0.08em] text-ink-secondary uppercase">Semantic Sheet</span>
        <h1 className="mt-6 font-display text-[32px] leading-tight text-ink">The server is not answering.</h1>
        <Notice tone="bad" className="mt-6">
          {wsError}. Start the API (<code className="font-mono">python -m semsheet.main</code>) and reload.
        </Notice>
      </div>
    );
  }
  if (!workspace) {
    return (
      <div className="flex h-full items-center justify-center gap-2 text-[13px] text-ink-muted">
        <Spinner /> Loading…
      </div>
    );
  }
  if (!dataset) {
    return (
      <Landing
        onDataset={(ds, n, suggested) => {
          setNote(n);
          setInitialPrompt(suggested);
          setDataset(ds);
          refresh();
        }}
        onPreview={setPreview}
      />
    );
  }
  return (
    <Workbench
      key={dataset.version_id}
      dataset={dataset}
      workspace={workspace}
      note={note}
      initialPrompt={initialPrompt}
      preview={preview}
      onBack={() => {
        setDataset(null);
        setPreview(null);
        setNote(undefined);
        setInitialPrompt(undefined);
        refresh();
      }}
      onWorkspaceRefresh={refresh}
    />
  );
}
