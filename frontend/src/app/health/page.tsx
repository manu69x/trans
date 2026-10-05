import Link from "next/link";

export default function HealthPage() {
  return (
    <main style={{ maxWidth: 720, margin: "4rem auto", padding: "0 1.5rem" }}>
      <h1>Health</h1>
      <p>Frontend is running.</p>
      <ul>
        <li>
          <a href="/api/v1/projects">Backend API (/api/v1/projects)</a>
        </li>
        <li>
          <a href="http://localhost:8000/health" target="_blank" rel="noreferrer">
            Backend /health
          </a>
        </li>
      </ul>
      <Link href="/">Home</Link>
    </main>
  );
}
