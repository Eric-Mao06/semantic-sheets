import { useEffect, useState } from "react";
import { api, getKey, setKey } from "./api";
import { Landing } from "./components/Landing";
import { Sheet } from "./components/Sheet";
import type { WorkspaceInfo } from "./types";

function parseRoute(): { datasetId: string | null; rv: string | null } {
  const m = location.hash.match(/^#\/d\/([^/?]+)(?:\?rv=([^&]+))?/);
  return { datasetId: m ? m[1] : null, rv: m && m[2] ? m[2] : null };
}

export function navigate(path: string) { location.hash = path; }

export function App() {
  const [route, setRoute] = useState(parseRoute());
  const [ws, setWs] = useState<WorkspaceInfo | null>(null);
  const [authError, setAuthError] = useState<string | null>(null);
  const [keyInput, setKeyInput] = useState(getKey());

  useEffect(() => {
    const onHash = () => setRoute(parseRoute());
    window.addEventListener("hashchange", onHash);
    return () => window.removeEventListener("hashchange", onHash);
  }, []);

  const load = () => api.workspace().then((w) => { setWs(w); setAuthError(null); }).catch((e) => setAuthError(e.message || "unauthorized"));
  useEffect(() => { load(); }, []);

  if (!ws) {
    return (
      <div className="login">
        <h1>Semantic Sheets</h1>
        <p>Enter the workspace key printed by the server (or set <code>SS_DEV_WORKSPACE_KEY</code>).</p>
        <form onSubmit={(e) => { e.preventDefault(); setKey(keyInput); load(); }}>
          <input value={keyInput} onChange={(e) => setKeyInput(e.target.value)} placeholder="workspace key" autoFocus />
          <button type="submit">Open workspace</button>
        </form>
        {authError && <p className="error">{authError}</p>}
      </div>
    );
  }
  if (route.datasetId) return <Sheet key={route.datasetId} datasetId={route.datasetId} initialRv={route.rv} workspace={ws} onWorkspaceChange={load} />;
  return <Landing workspace={ws} />;
}
