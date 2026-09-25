import { useEffect, useState } from "react";
import { api, appConfig, post, Project, User } from "../api";
import type { Nav } from "../App";
import { Button, Classification, Empty, Field, Modal, PageHead, useAction } from "../ui";

export default function ProjectsPage({ user, nav }: { user: User; nav: Nav }) {
  const [items, setItems] = useState<Project[] | null>(null);
  const [open, setOpen] = useState(false);
  const { busy, run } = useAction();
  const canBuild = user.roles.includes("Admin") || user.roles.includes("Builder");
  const canChooseReview = appConfig.public_mode || user.roles.includes("Admin");
  const load = () => api<Project[]>("/projects").then(setItems).catch(() => setItems([]));
  useEffect(() => { load(); }, []);

  return (
    <>
      <PageHead title="Projects" sub="A project holds knowledge bases, agent workflows and published assistants. Access is by explicit membership."
        actions={canBuild && <Button kind="primary" onClick={() => setOpen(true)}>New project</Button>} />
      {items === null ? <p className="muted">Loading…</p> : items.length === 0 ? (
        <Empty title="No projects yet">{canBuild ? "Create a project, add a knowledge base and upload your manuals." :
          "Ask a project owner to add you to a project."}</Empty>
      ) : (
        <div className="grid3">
          {items.map((p) => (
            <button key={p.id} className="tile" onClick={() => nav(`project/${p.id}/knowledge`)}>
              <div className="row between"><span className="tile-title">{p.name}</span><Classification c={p.classification_floor} /></div>
              <div className="muted small" style={{ minHeight: 20 }}>{p.description || "No description"}</div>
              <div className="stats"><span>{p.knowledge_bases} knowledge bases</span><span>{p.workflows} agents</span>
                <span>{p.assistants} published</span></div>
              <div className="small muted">Your access: <b>{p.membership}</b></div>
            </button>
          ))}
        </div>
      )}
      {open && (
        <Modal title="New project" onClose={() => setOpen(false)}>
          <form onSubmit={async (e) => {
            e.preventDefault(); const f = new FormData(e.currentTarget);
            const r = await run(() => post<{ id: string }>("/projects", { name: f.get("name"), description: f.get("description"),
              classification_floor: f.get("cls"), ...(canChooseReview ? { require_review: f.get("rr") === "on" } : {}) }), "Project created");
            if (r) nav(`project/${r.id}/knowledge`);
          }}>
            <Field label="Name"><input name="name" required maxLength={120} placeholder="e.g. CDU-1 operations assistant" /></Field>
            <Field label="Description"><textarea name="description" maxLength={2000} /></Field>
            <Field label="Minimum classification" hint="Knowledge bases in this project cannot be classified lower than this.">
              <select name="cls" defaultValue={appConfig.public_mode ? "Public" : "Internal"}><option>Public</option><option>Internal</option>
                {!appConfig.public_mode && <option>Restricted</option>}</select>
            </Field>
            {canChooseReview && <label className="row" style={{ marginBottom: 12, alignItems: "flex-start" }}>
              <input type="checkbox" name="rr" defaultChecked={!appConfig.public_mode} />
              <span>Require Answer Review for every agent<div className="muted small">Recommended for safety-critical content. When off, agents may
                use a Direct Output node and answers are shown without the citation and number checks.</div></span></label>}
            <div className="modal-foot"><Button type="button" onClick={() => setOpen(false)}>Cancel</Button>
              <Button kind="primary" disabled={busy}>Create</Button></div>
          </form>
        </Modal>
      )}
    </>
  );
}
