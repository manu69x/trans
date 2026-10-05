/**
 * Route handler Next.js: proxy `/api/v1/*` verso il backend FastAPI.
 *
 * Stesso pattern del proxy Gateway (`/api/gateway/*`): il browser parla SOLO
 * con l'origine Next (same-origin, nessun CORS); il server Next inoltra al
 * backend via compose network (BACKEND_URL, default http://backend:8000).
 * Le `rewrites()` in next.config.js dipendono da un env valutato al BUILD,
 * che nel container non era impostato → 404. Questo route handler risolve
 * il routing a runtime, senza rebuild.
 */

import { NextRequest, NextResponse } from "next/server";

const BACKEND_URL = process.env.BACKEND_URL || "http://backend:8000";

async function forward(
  request: NextRequest,
  pathParts: string[]
): Promise<NextResponse> {
  const target = `${BACKEND_URL}/api/v1/${pathParts.join("/")}${
    request.nextUrl.search || ""
  }`;
  const headers = new Headers();
  headers.set("content-type", request.headers.get("content-type") || "application/json");
  if (request.headers.get("authorization")) {
    headers.set("authorization", request.headers.get("authorization") as string);
  }

  const init: RequestInit = { method: request.method, headers };
  if (!["GET", "HEAD"].includes(request.method)) {
    init.body = await request.arrayBuffer();
  }
  const resp = await fetch(target, init);
  const body = await resp.arrayBuffer();
  return new NextResponse(body, {
    status: resp.status,
    headers: {
      "content-type": resp.headers.get("content-type") || "application/json",
    },
  });
}

export async function GET(
  request: NextRequest,
  ctx: { params: Promise<{ path: string[] }> }
) {
  const { path } = await ctx.params;
  return forward(request, path);
}

export async function POST(
  request: NextRequest,
  ctx: { params: Promise<{ path: string[] }> }
) {
  const { path } = await ctx.params;
  return forward(request, path);
}

export async function PATCH(
  request: NextRequest,
  ctx: { params: Promise<{ path: string[] }> }
) {
  const { path } = await ctx.params;
  return forward(request, path);
}

export async function PUT(
  request: NextRequest,
  ctx: { params: Promise<{ path: string[] }> }
) {
  const { path } = await ctx.params;
  return forward(request, path);
}

export async function DELETE(
  request: NextRequest,
  ctx: { params: Promise<{ path: string[] }> }
) {
  const { path } = await ctx.params;
  return forward(request, path);
}
