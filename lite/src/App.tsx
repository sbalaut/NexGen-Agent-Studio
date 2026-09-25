import { useEffect, useState } from "react";
import { DataProvider, useData } from "./data";
import { ToastHost } from "./ui";
import ChatPage from "./pages/Chat";
import AgentsPage from "./pages/Agents";
import KnowledgePage from "./pages/Knowledge";
import ToolsPage from "./pages/Tools";
import SettingsPage from "./pages/Settings";

export type Nav = (path: string) => void;

function useHash(): [string[], Nav] {
  const read = () => (location.hash.replace(/^#\/?/, "") || "chat").split("/");
  const [parts, setParts] = useState(read());
  useEffect(() => { const f = () => setParts(read()); window.addEventListener("hashchange", f); return () => window.removeEventListener("hashchange", f); }, []);
  return [parts, (p) => { location.hash = "/" + p; }];
}

const NAV: [string, string][] = [["chat", "Chat with agents"], ["agents", "Agent builder"], ["knowledge", "Knowledge & GraphRAG"],
  ["tools", "MCP tools"], ["settings", "API keys & data"]];

function Shell() {
  const [[view, a, b], nav] = useHash();
  const { connections, loaded } = useData();
  const [menu, setMenu] = useState(false);
  return (
    <div className="shell">
      <aside className={"side" + (menu ? " open" : "")} aria-label="Main navigation">
        <div className="logo"><span className="logo-mark">N</span>NexAgent Lite</div>
        {NAV.map(([k, label]) => (
          <button key={k} className={"nav-item" + (view === k ? " active" : "")} onClick={() => { nav(k); setMenu(false); }}>
            {label}{k === "settings" && !connections.length && <span className="count">!</span>}</button>))}
        <div className="side-foot">
          <p className="small muted" style={{ margin: 0 }}>Runs entirely in your browser. Documents and agents are stored on this device; API calls go
            straight from here to the providers and MCP servers you add.</p>
        </div>
      </aside>
      <button className="menu-toggle btn" onClick={() => setMenu(!menu)} aria-label="Menu">☰</button>
      <main className="main">
        {!loaded ? <p className="muted">Loading…</p> : <>
          {!connections.length && view !== "settings" && (
            <div className="notice warn" style={{ marginBottom: 16 }}>Start by adding an API key (OpenAI, Anthropic or Gemini) under{" "}
              <button className="linklike" onClick={() => nav("settings")}>API keys &amp; data</button>.</div>)}
          {view === "chat" && <ChatPage wid={a} nav={nav} />}
          {view === "agents" && <AgentsPage wid={a} nav={nav} />}
          {view === "knowledge" && <KnowledgePage kid={a} tab={b} nav={nav} />}
          {view === "tools" && <ToolsPage />}
          {view === "settings" && <SettingsPage />}
        </>}
      </main>
    </div>
  );
}

export default function App() {
  return <ToastHost><DataProvider><Shell /></DataProvider></ToastHost>;
}
