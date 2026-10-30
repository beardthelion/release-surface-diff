export async function GET(req: Request, { params }: { params: { id: string } }) {
  return Response.json({ id: params.id });
}

export async function POST(req: Request, { params }: { params: { id: string } }) {
  return Response.json({ id: params.id, created: true });
}
