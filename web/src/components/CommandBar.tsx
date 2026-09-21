import { useState } from "react";

export function CommandBar({ busy, onCompile, placeholder, suggestions }: { busy: boolean; onCompile: (req: string) => void; placeholder: string; suggestions: string[] }) {
  const [text, setText] = useState("");
  return (
    <div className="commandbar">
      <input value={text} onChange={(e) => setText(e.target.value)} placeholder={placeholder} list="ss-suggestions" data-testid="command-input"
        onKeyDown={(e) => { if (e.key === "Enter" && text.trim() && !busy) onCompile(text.trim()); }} />
      <datalist id="ss-suggestions">{suggestions.map((s) => <option key={s} value={s} />)}</datalist>
      <button className="primary" disabled={busy || !text.trim()} onClick={() => onCompile(text.trim())} data-testid="command-plan">{busy ? "Planning…" : "Plan"}</button>
    </div>
  );
}
