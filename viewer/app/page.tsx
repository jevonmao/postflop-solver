import Link from 'next/link';
import { listMatchups, dataPaths } from '@/lib/data';

export const dynamic = 'force-dynamic';

const TOTAL_CANONICAL_FLOPS = 1755;

const SUBTITLES: Record<string, string> = {
  SRP: 'BTN open · BB call · SPR ~40',
  '3BP': 'BB 3-bet · BTN call · SPR ~9.5',
  '4BP': 'BTN 4-bet · BB call · SPR ~3.5',
};

export default async function Home() {
  const matchups = await listMatchups();
  const { SOLVES_DIR } = dataPaths();

  return (
    <div className="space-y-6">
      <div>
        <h1 className="text-2xl font-semibold text-neutral-100">Solved spots</h1>
        <p className="text-sm text-neutral-500 mt-1">Reading from <code className="bg-neutral-900 px-1 rounded">{SOLVES_DIR}</code></p>
      </div>

      <div className="grid grid-cols-1 md:grid-cols-3 gap-4">
        {matchups.map(({ label, count, partial }) => {
          const empty = count === 0;
          return (
            <Link
              key={label}
              href={empty ? '#' : `/spots/${label}`}
              className={`block rounded-lg border p-5 transition ${
                empty
                  ? 'border-neutral-900 bg-neutral-950 text-neutral-600 cursor-not-allowed'
                  : 'border-neutral-800 bg-neutral-900 hover:bg-neutral-800 hover:border-neutral-700'
              }`}
            >
              <div className="flex items-baseline justify-between">
                <span className="text-xl font-mono font-semibold tracking-wider">{label}</span>
                <span className="text-xs text-neutral-500">{SUBTITLES[label]}</span>
              </div>
              <div className="mt-3 text-3xl font-bold">
                {count}
                <span className="text-neutral-500 text-lg font-normal">
                  {' '}/ {TOTAL_CANONICAL_FLOPS}
                </span>
              </div>
              <div className="mt-1 text-xs text-neutral-500">
                {partial > 0 && `${partial} partial · `}
                {((100 * count) / TOTAL_CANONICAL_FLOPS).toFixed(1)}% complete
              </div>
            </Link>
          );
        })}
      </div>
    </div>
  );
}
