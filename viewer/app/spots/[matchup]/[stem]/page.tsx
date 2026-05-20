import Link from 'next/link';
import { notFound } from 'next/navigation';
import { loadSpot } from '@/lib/data';
import { MATCHUPS, type Matchup } from '@/lib/types';
import { SpotViewer } from '@/components/SpotViewer';

export const dynamic = 'force-dynamic';

interface PageProps { params: { matchup: string; stem: string } }

export default async function SpotPage({ params }: PageProps) {
  const matchup = params.matchup as Matchup;
  if (!MATCHUPS.includes(matchup)) notFound();

  let payload;
  try {
    payload = await loadSpot(matchup, params.stem);
  } catch (e) {
    return (
      <div className="space-y-4">
        <Link href={`/spots/${matchup}`} className="text-xs text-neutral-500 hover:text-neutral-300">← back</Link>
        <p className="text-red-400">Failed to load spot: {(e as Error).message}</p>
      </div>
    );
  }

  return (
    <div className="space-y-4">
      <div className="flex items-center gap-3 text-xs">
        <Link href="/" className="text-neutral-500 hover:text-neutral-300">all</Link>
        <span className="text-neutral-700">/</span>
        <Link href={`/spots/${matchup}`} className="text-neutral-500 hover:text-neutral-300">{matchup}</Link>
        <span className="text-neutral-700">/</span>
        <span className="text-neutral-300 font-mono">{params.stem}</span>
      </div>
      <SpotViewer
        matchup={matchup}
        stem={params.stem}
        records={payload.records}
        meta={payload.meta}
      />
    </div>
  );
}
