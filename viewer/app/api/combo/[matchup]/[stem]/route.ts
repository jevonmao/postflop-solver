import { NextRequest, NextResponse } from 'next/server';
import { loadNodeCombo } from '@/lib/data';
import { MATCHUPS, type Matchup } from '@/lib/types';

export const dynamic = 'force-dynamic';

interface Ctx { params: { matchup: string; stem: string } }

export async function GET(req: NextRequest, { params }: Ctx) {
  const matchup = params.matchup as Matchup;
  if (!MATCHUPS.includes(matchup)) {
    return NextResponse.json({ error: 'unknown matchup' }, { status: 404 });
  }

  let history: string[] = [];
  const h = req.nextUrl.searchParams.get('h');
  if (h) {
    try {
      const parsed = JSON.parse(h);
      if (Array.isArray(parsed)) history = parsed.map(String);
    } catch {
      return NextResponse.json({ error: 'bad history param' }, { status: 400 });
    }
  }

  try {
    const node = await loadNodeCombo(matchup, params.stem, history);
    if (!node) {
      return NextResponse.json({ error: 'no combo data for this node' }, { status: 404 });
    }
    return NextResponse.json(node);
  } catch (e) {
    return NextResponse.json({ error: (e as Error).message }, { status: 500 });
  }
}
