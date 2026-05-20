const SUIT_GLYPH: Record<string, string> = { c: '♣', d: '♦', h: '♥', s: '♠' };
const SUIT_COLOR: Record<string, string> = {
  c: 'text-neutral-100',
  d: 'text-rose-400',
  h: 'text-rose-400',
  s: 'text-neutral-100',
};

export function Card({ card, size = 'md' }: { card: string; size?: 'sm' | 'md' | 'lg' }) {
  const rank = card[0];
  const suit = card[1];
  const sz =
    size === 'lg' ? 'text-2xl w-10 h-14' :
    size === 'sm' ? 'text-xs  w-6  h-8'  :
                    'text-base w-8 h-11';
  return (
    <span
      className={`inline-flex items-center justify-center rounded border border-neutral-700 bg-neutral-900 font-mono font-semibold ${SUIT_COLOR[suit] ?? 'text-neutral-300'} ${sz}`}
      title={card}
    >
      <span>{rank}</span>
      <span>{SUIT_GLYPH[suit] ?? suit}</span>
    </span>
  );
}

export function Board({ cards, size = 'md' }: { cards: string[]; size?: 'sm' | 'md' | 'lg' }) {
  return (
    <div className="flex gap-1">
      {cards.map((c, i) => <Card key={i} card={c} size={size} />)}
    </div>
  );
}
