// Provides a small HTTP route for the Next.js example project.
// Returns a successful JSON response so the scanner can recognize an app with an API.
export function GET(): Response {
  return Response.json({ ok: true });
}
