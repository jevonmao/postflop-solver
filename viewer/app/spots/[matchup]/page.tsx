import Link from 'next/link';
import { notFound } from 'next/navigation';
import { listSpots } from '@/lib/data';
import { MATCHUPS, type Matchup } from '@/lib/types';
import { Board } from '@/components/Card';

export const dynamic = 'force-dynamic';

interface PageProps { params: { matchup: string } }

export default async function SpotsList({ params }: PageProps) {
  const matchup = params.matchup as Matchup;
  if (!MATCHUPS.includes(matchup)) notFound();

  const spots = await listSpots(matchup);

  return (
    <div className="space-y-6">
      <div className="flex items-center justify-between">
        <div>
          <Link href="/" className="text-xs text-neutral-500 hover:text-neutral-300">← all matchups</Link>
          <h1 className="text-2xl font-semibold mt-1">{matchup} spots</h1>
          <p className="text-sm text-neutral-500">{spots.length} solved flops</p>
        </div>
      </div>

      {spots.length === 0 ? (
        <p className="text-neutral-500 italic">No spots solved yet for this matchup.</p>
      ) : (
        <div className="grid grid-cols-1 md:grid-cols-2 lg:grid-cols-3 xl:grid-cols-4 gap-3">
          {spots.map((s) => (
            <Link
              key={s.stem}
              href={`/spots/${matchup}/${s.stem}`}
              className="rounded-md border border-neutral-800 bg-neutral-900 hover:bg-neutral-800 hover:border-neutral-700 px-3 py-3 flex items-center gap-3 transition"
            >
              <span className="text-xs text-neutral-500 font-mono w-12 shrink-0">{String(s.idx).padStart(4, '0')}</span>
              <Board cards={parseFlop(s.flop)} />
              <div className="ml-auto text-right text-xs text-neutral-500 space-y-0.5">
                {s.meta ? (
                  <>
                    <div>{s.meta.exploitability_pct_pot.toFixed(2)}% expl</div>
                    <div>{s.meta.solve_s.toFixed(1)}s · {s.meta.n_records} rec</div>
                    {s.meta.combo_data && <div className="text-emerald-500">combo</div>}
                  </>
                ) : (
                  <div className="text-amber-500">missing meta</div>
                )}
              </div>
            </Link>
          ))}
        </div>
      )}
    </div>
  );
}

function parseFlop(s: string): string[] {
  return [s.slice(0, 2), s.slice(2, 4), s.slice(4, 6)];
}
