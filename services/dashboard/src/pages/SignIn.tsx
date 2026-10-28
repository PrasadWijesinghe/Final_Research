import { Github } from "lucide-react";
import { LOGIN_URL } from "../lib/api";
import { Link } from "../lib/router";

export default function SignIn() {
  return (
    <section className="pq-signin">
      <p className="kicker">PipelineIQ</p>
      <h2>Sign in to see your pipeline risk</h2>
      <p className="pq-dim">
        PipelineIQ predicts and explains failures in your Node.js GitHub Actions pipelines. Sign in with GitHub to
        see predictions, review diagnoses and manage your API keys.
      </p>
      <a className="pq-btn pq-primary" href={LOGIN_URL}>
        <Github size={16} /> Sign in with GitHub
      </a>
      <p className="pq-dim">
        Just looking? The <Link to="/infra">Infra demo</Link> works without signing in.
      </p>
    </section>
  );
}
