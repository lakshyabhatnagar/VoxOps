// Why: Supply evidence of a single Next.js application's HTTP capability.
// How: Export a route handler that is read as text and never executed by the scanner.
export function GET(): Response {
  return Response.json({ ok: true });
}
