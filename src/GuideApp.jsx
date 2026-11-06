import React, { useEffect, useState } from "react";

const sections = [
  { id: "overview", group: "Setup", icon: "🏠", label: "Overview" },
  { id: "local", group: "Setup", icon: "🖥️", label: "Local Setup" },
  { id: "cloud", group: "Compute", icon: "☁️", label: "Cloud (AWS/Azure/GCP)" },
  { id: "hpc", group: "Compute", icon: "🏛️", label: "HPC (Slurm/PBS)" },
  { id: "llm", group: "AI / LLM", icon: "🤖", label: "Ollama · Claude · GPT" },
  { id: "workbench", group: "Platform", icon: "⚡", label: "Workbench Tour" },
  { id: "workflow", group: "Platform", icon: "🧬", label: "First Workflow" },
  { id: "faq", group: "Platform", icon: "❓", label: "FAQ" },
];

const modes = [
  ["💻", "Local", "Run Studio and its services on your workstation."],
  ["☁️", "Cloud", "Connect approved AWS, Azure, or GCP execution backends."],
  ["🏛️", "HPC", "Submit work through an institution-managed scheduler."],
  ["🔀", "Hybrid", "Keep sensitive data local while using remote compute."],
];

const services = ["Workbench", "Control Center", "TES", "Workflow Catalog", "Tool Images", "Model Registry", "RAG", "LIMS", "Video Tutorials"];

function Steps({ items }) {
  return <div className="panel"><div className="panel-body">{items.map((item, index) => <div className="step" key={item[0]}><span className="step-num">{index + 1}</span><div><h3 className="step-title">{item[0]}</h3><p className="step-desc">{item[1]}</p></div></div>)}</div></div>;
}

function Page({ id, title, subtitle, children, active }) {
  return <section id={id} className={`guide-page${active ? " active" : ""}`} aria-labelledby={`${id}-title`} hidden={!active}>
    <h1 id={`${id}-title`} className="page-title">{title}</h1><p className="page-sub">{subtitle}</p>{children}
  </section>;
}

export default function GuideApp() {
  const initial = window.location.hash.slice(1);
  const [active, setActive] = useState(sections.some((section) => section.id === initial) ? initial : "overview");
  useEffect(() => {
    const onHash = () => {
      const id = window.location.hash.slice(1);
      if (sections.some((section) => section.id === id)) setActive(id);
    };
    window.addEventListener("hashchange", onHash);
    return () => window.removeEventListener("hashchange", onHash);
  }, []);
  function show(id) {
    setActive(id);
    window.history.replaceState(null, "", `#${id}`);
    document.getElementById("guide-main")?.focus();
  }
  const groups = [...new Set(sections.map((section) => section.group))];
  return <>
    <header className="guide-topbar"><div className="topbar-path"><span>studio</span><span className="sep">/</span><span className="current">getting-started</span></div><div className="topbar-badges"><span className="pill pill-green">● GUIDE</span><span className="pill pill-blue">React + Vite</span><span className="pill pill-orange">BETA</span></div></header>
    <div className="guide-layout">
      <nav className="sidebar" aria-label="Getting started sections">{groups.map((group) => <React.Fragment key={group}><div className="sidebar-section">{group}</div>{sections.filter((section) => section.group === group).map((section) => <button key={section.id} type="button" className={`sidebar-link${active === section.id ? " active" : ""}`} aria-current={active === section.id ? "page" : undefined} onClick={() => show(section.id)}><span className="sidebar-icon" aria-hidden="true">{section.icon}</span>{section.label}</button>)}</React.Fragment>)}</nav>
      <main id="guide-main" className="guide-main" tabIndex={-1}>
        <Page id="overview" active={active === "overview"} title="Welcome to OmniBioAI" subtitle="Choose the execution model that fits your research environment.">
          <div className="grid2">{modes.map(([icon, title, description]) => <article className="card" key={title}><div className="card-icon">{icon}</div><h2 className="card-title">{title}</h2><p className="card-desc">{description}</p></article>)}</div>
          <div className="tip">Start locally with synthetic data. Add remote compute only after identity, storage, and execution policies are approved.</div>
        </Page>
        <Page id="local" active={active === "local"} title="Local setup" subtitle="Install Studio and verify the local service stack before running analyses.">
          <Steps items={[["Install prerequisites", "Install Docker Desktop or an approved container runtime and allocate sufficient CPU, memory, and disk."], ["Install OmniBioAI Studio", "Use the signed package for your operating system and preserve its release metadata."], ["Choose data directories", "Keep inputs, results, and cache locations explicit and backed up."], ["Run the health check", "Confirm required services are healthy before submitting work."]]} />
          <pre className="cmd"><code>docker version{"\n"}docker compose ps</code></pre>
        </Page>
        <Page id="cloud" active={active === "cloud"} title="Cloud compute" subtitle="Connect approved cloud execution and storage accounts.">
          <Steps items={[["Select a provider", "Use the organization-approved AWS, Azure, or GCP account and region."], ["Create least-privilege identity", "Grant only the execution and artifact permissions required by the workflow."], ["Configure storage", "Choose encrypted inputs, outputs, logs, and lifecycle policies."], ["Test with synthetic data", "Verify submission, cancellation, audit, and cleanup before research data is used."]]} />
          <div className="warn">Never paste long-lived cloud secrets into a workflow, video, manifest, or browser URL.</div>
        </Page>
        <Page id="hpc" active={active === "hpc"} title="HPC schedulers" subtitle="Connect Studio to Slurm, PBS, or LSF through an approved gateway.">
          <Steps items={[["Confirm scheduler support", "Ask the cluster owner which scheduler, queues, and resource limits are approved."], ["Configure SSH", "Use a dedicated key or approved short-lived credential and verify the host fingerprint."], ["Map storage", "Ensure compute nodes can access the declared working and result directories."], ["Submit a smoke job", "Test cancellation, timeout, exit codes, logs, and cleanup with synthetic input."]]} />
        </Page>
        <Page id="llm" active={active === "llm"} title="AI and language models" subtitle="Choose local or hosted models without exposing secrets or sensitive data.">
          <div className="grid2"><article className="card"><div className="card-icon">🏠</div><h2 className="card-title">Local Ollama</h2><p className="card-desc">Keeps prompts on the workstation when the selected model and hardware are sufficient.</p></article><article className="card"><div className="card-icon">🔐</div><h2 className="card-title">Hosted API</h2><p className="card-desc">Requires an approved provider, data-handling policy, secret store, and model allowlist.</p></article></div>
          <div className="warn">Do not enter API keys into tutorial fields or commit them to configuration files.</div>
        </Page>
        <Page id="workbench" active={active === "workbench"} title="Workbench tour" subtitle="The workspace connects scientific tools to governed platform services.">
          <div className="svc-grid">{services.map((service) => <div className="svc-tile" key={service}><div className="svc-tile-icon">◆</div><div className="svc-tile-name">{service}</div><div className="svc-tile-port">platform service</div></div>)}</div>
          <div className="tip">A visible tile is not proof that its backend is healthy or that your identity is authorized. Check service status and permissions.</div>
        </Page>
        <Page id="workflow" active={active === "workflow"} title="Run your first workflow" subtitle="Use approved synthetic input for the first end-to-end qualification.">
          <Steps items={[["Choose a workflow", "Select an approved immutable workflow version from the catalog."], ["Validate inputs", "Confirm formats, reference versions, provenance, and required metadata."], ["Review execution", "Inspect tool versions, resources, permissions, storage destinations, and estimated cost."], ["Submit and monitor", "Watch state transitions, logs, cancellation, audit events, and artifact creation."], ["Validate results", "Compare outputs to the workflow's approved expected results before reuse."]]} />
        </Page>
        <Page id="faq" active={active === "faq"} title="Frequently asked questions" subtitle="Common setup and execution issues.">
          <div className="faq-list">{[["Why is a service unavailable?", "Check Studio health, container status, network policy, and the owning service's logs."], ["Why was execution denied?", "Confirm your tenant, identity, permission, policy decision, and session freshness."], ["Why is a workflow pending?", "Inspect scheduler capacity, quotas, image pulls, storage access, and dependency health."], ["Where are results stored?", "Use the execution record and artifact provenance; do not infer a path from a filename."], ["Can I use patient data?", "Only in an explicitly approved environment with the required privacy, security, and governance controls."]].map(([question, answer]) => <details key={question}><summary>{question}</summary><p>{answer}</p></details>)}</div>
        </Page>
      </main>
    </div>
  </>;
}
