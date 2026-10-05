/**
 * Next.js route handler: proxy to the LLM Gateway (PRD §12.5).
 *
 * The browser calls `/api/gateway/*`; the Next server forwards to the
 * gateway. `LLM_GATEWAY_URL` stays a server-side secret and the local_only
 * policy (§13.1) is enforced in a single place: the gateway is always a
 * local endpoint.
 */

import { NextRequest, NextResponse } from "next/server";

const LLM_GATEWAY_URL =
  process.env.LLM_GATEWAY_URL || "http://127.0.0.1:8080";

function isLocalHost(hostname: string): boolean {
  return (
    hostname === "localhost" ||
    hostname === "127.0.0.1" ||
    hostname === "::1" ||
    hostname === "0.0.0.0" ||
    hostname.endsWith(".local")
  );
}

async function forward(
  request: NextRequest,
  pathParts: string[]
): Promise<NextResponse> {
  const target = `${LLM_GATEWAY_URL}/${pathParts.join("/")}${
    request.nextUrl.search || ""
  }`;

  // §13.1 local_only: block any non-local target.
  const hostname = new URL(target).hostname;
  if (!isLocalHost(hostname)) {
    return NextResponse.json(
      { detail: `non-local endpoint blocked (local_only): ${hostname}` },
      { status: 403 }
    );
  }

  try {
    const res = await fetch(target, {
      method: request.method,
      headers: { Accept: "application/json" },
      body:
        request.method === "GET" || request.method === "HEAD"
          ? undefined
          : await request.text(),
      cache: "no-store",
    });
    const text = await res.text();
    return new NextResponse(text, {
      status: res.status,
      headers: {
        "Content-Type": res.headers.get("Content-Type") || "application/json",
      },
    });
  } catch {
    return NextResponse.json(
      { detail: "LLM Gateway unreachable" },
      { status: 502 }
    );
  }
}

type Ctx = { params: { path: string[] } };

export async function GET(request: NextRequest, ctx: Ctx) {
  return forward(request, ctx.params.path);
}

export async function POST(request: NextRequest, ctx: Ctx) {
  return forward(request, ctx.params.path);
}
