import Link from "next/link";
import { ArrowRight, ArrowUpRight, Check, FileText, FolderOpen, GitBranch, Globe2, History, Laptop2, ListTodo, Smartphone, Terminal } from "lucide-react";

const features = [
  { number: "02", icon: GitBranch, title: "Pick up where work left off.", label: "Agent coordination", copy: "Keep task ownership, dependencies and the next step together. Save a handoff when work pauses, so the next agent can pick up the context.", detail: "Owners · Dependencies · Handoffs" },
  { number: "03", icon: Globe2, title: "Work in the browser you use.", label: "Browser control", copy: "Let connected agents inspect and work with permitted website tabs through the Megas Moves browser extension. Reuse open tabs and keep finished work tidy.", detail: "Tabs · Page controls · Screenshots" },
  { number: "04", icon: Terminal, title: "Your tools. In the same picture.", label: "Terminals and local testing", copy: "Follow terminal sessions, check local services and see which ports are ready. Bring logs and test results back to the work they belong to.", detail: "Sessions · Services · Readiness" },
  { number: "05", icon: History, title: "See what happened. And what’s next.", label: "Activity and evidence", copy: "Review recorded operations and failures alongside task context. Keep the result, the evidence and the next action visible to people and agents.", detail: "Activity · Results · Context" },
];

export default function ProductFeatures() {
  return <section id="features" className="product-features" aria-labelledby="features-title">
    <div className="section-heading">
      <p className="eyebrow">Inside Megas Moves</p>
      <h2 id="features-title">Built for the work<br />between prompts.</h2>
      <Link href="/docs" className="text-link">Explore the capabilities<ArrowRight aria-hidden="true" /></Link>
    </div>
    <div className="product-feature-lead">
      <div className="product-feature-lead__copy">
        <span className="product-feature-number">01 / Actions &amp; tasks</span>
        <h3>One Action.<br /><strong>Every moving part.</strong></h3>
        <p>Give your work a home. Gather the folders, files and tasks for an outcome into a Megas Action. Your desktop and connected agents see the same work.</p>
        <Link href="/docs/getting-started" className="text-link">Start with an Action<ArrowUpRight aria-hidden="true" /></Link>
      </div>
      <figure className="action-illustration" aria-label="Example of an Action grouping references and tasks">
        <div className="action-illustration__title"><FolderOpen aria-hidden="true" /><span><small>Megas Action</small><strong>Launch the website</strong></span></div>
        <div className="action-illustration__columns">
          <div><span className="action-illustration__label">References</span><span><FolderOpen aria-hidden="true" />Website source</span><span><FileText aria-hidden="true" />Product brief</span><span><FolderOpen aria-hidden="true" />Release assets</span></div>
          <div><span className="action-illustration__label">Tasks</span><span><Check aria-hidden="true" />Review content</span><span><ListTodo aria-hidden="true" />Check downloads</span><span><ListTodo aria-hidden="true" />Prepare release</span></div>
        </div>
        <figcaption>One Action, multiple references and tasks. Illustrative workflow.</figcaption>
      </figure>
    </div>
    <div className="product-feature-grid">{features.map(({number,icon:Icon,title,label,copy,detail}) => <article className="product-feature" key={number}>
      <div className="product-feature__label"><Icon aria-hidden="true" /><span>{label}</span><span>{number}</span></div>
      <h3>{title}</h3><p>{copy}</p><span className="product-feature__detail">{detail}</span>
    </article>)}</div>
    <div className="product-device-strip"><div><Laptop2 aria-hidden="true" /><Smartphone aria-hidden="true" /></div><p><strong>Keep your devices in view.</strong> Inspect connected Android devices and local runtime status from the Windows operator.</p><Link href="/android-testing" className="text-link">Device testing<ArrowRight aria-hidden="true" /></Link></div>
    <p className="product-feature-scope">Capabilities shown for the Windows operator. Browser control requires the extension. See Downloads for builds on other platforms.</p>
  </section>;
}
