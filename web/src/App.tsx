import { useCallback, useEffect, useState } from "react";
import { api } from "./api";
import Landing, { type Preview } from "./components/Landing";
import Workbench from "./components/Workbench";
import type { DatasetInfo, WorkspaceInfo } from "./types";

export default function App() {
  const [workspace, setWorkspace] = useState<WorkspaceInfo | null>(null);
  const [wsError, setWsError] = useState<string | null>(null);
  const [dataset, setDataset] = useState<DatasetInfo | null>(null);
  const [note, setNote] = useState<string | undefined>(undefined);
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
      <div className="landing">
        <h1>Semantic Spreadsheet</h1>
        <div className="error" style={{ marginTop: 16 }}>
          Cannot reach the API: {wsError}. Start the server (<code>uvicorn semsheet.main:app</code>) and reload.
        </div>
      </div>
    );
  }
  if (!workspace) {
    return (
      <div className="landing">
        <span className="spinner" /> loading workspace…
      </div>
    );
  }
  if (!dataset) {
    return (
      <Landing
        onDataset={(ds, n) => {
          setNote(n);
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
      preview={preview}
      onBack={() => {
        setDataset(null);
        setPreview(null);
        setNote(undefined);
        refresh();
      }}
      onWorkspaceRefresh={refresh}
    />
  );
}
