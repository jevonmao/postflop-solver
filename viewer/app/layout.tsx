import './globals.css';
import type { ReactNode } from 'react';
import Link from 'next/link';

export const metadata = {
  title: 'Postflop Solver Viewer',
  description: 'Preview solved HU 200BB postflop dataset spots',
};

export default function RootLayout({ children }: { children: ReactNode }) {
  return (
    <html lang="en">
      <body className="min-h-screen">
        <header className="border-b border-neutral-800 bg-neutral-950">
          <div className="mx-auto max-w-7xl px-4 py-3 flex items-center gap-4">
            <Link href="/" className="font-semibold text-neutral-100 hover:text-white">
              Postflop Viewer
            </Link>
            <span className="text-xs text-neutral-500">
              HU 200BB · stratified canonical-flop dataset
            </span>
          </div>
        </header>
        <main className="mx-auto max-w-7xl px-4 py-6">{children}</main>
      </body>
    </html>
  );
}
