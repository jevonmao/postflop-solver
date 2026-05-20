import type { NodeRecord } from './types';

export interface TrieNode {
  record: NodeRecord | null;
  children: Map<string, TrieNode>;
  /** Key in the parent's children map (history step that led here). */
  key: string;
  /** Full history from root to this node. */
  fullHistory: string[];
}

function makeNode(key: string, fullHistory: string[]): TrieNode {
  return { record: null, children: new Map(), key, fullHistory };
}

export function buildTrie(records: NodeRecord[]): TrieNode {
  const root = makeNode('', []);
  for (const r of records) {
    let cur = root;
    for (let i = 0; i < r.history.length; i++) {
      const step = r.history[i];
      let next = cur.children.get(step);
      if (!next) {
        next = makeNode(step, r.history.slice(0, i + 1));
        cur.children.set(step, next);
      }
      cur = next;
    }
    cur.record = r;
  }
  return root;
}

export function navigate(root: TrieNode, history: string[]): TrieNode | null {
  let cur: TrieNode | null = root;
  for (const step of history) {
    if (!cur) return null;
    const next: TrieNode | undefined = cur.children.get(step);
    cur = next ?? null;
  }
  return cur;
}

/** True if a node represents a chance node (no record, has deal_X children). */
export function isChanceNode(node: TrieNode): boolean {
  if (node.record) return false;
  for (const k of node.children.keys()) {
    if (!k.startsWith('deal_')) return false;
  }
  return node.children.size > 0;
}
