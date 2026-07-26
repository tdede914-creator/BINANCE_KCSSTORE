"use client";

import { useEffect, useMemo, useState } from "react";
import clsx from "clsx";

interface WatchlistEditorProps {
  /** Current committed list (server-authoritative). */
  initial: string[];
  /** Called when the user clicks Save; must persist the new list server-side. */
  onSave: (list: string[]) => Promise<void>;
  /** Input placeholder — should hint at the expected symbol format. */
  placeholder?: string;
  /** Suggested symbols shown as one-click chips above the input. Optional. */
  suggestions?: string[];
  disabled?: boolean;
}

/**
 * Pill-based watchlist editor.
 *
 * - Each coin is rendered as a chip with an × to remove.
 * - Type a symbol in the input and press Enter (or click Add) to append it.
 * - Save / Cancel buttons appear only when there are unsaved changes.
 * - Duplicate entries are prevented at the local level so we never send them.
 */
export function WatchlistEditor({
  initial,
  onSave,
  placeholder = "Add a symbol",
  suggestions,
  disabled,
}: WatchlistEditorProps) {
  const [items, setItems] = useState<string[]>(initial);
  const [draft, setDraft] = useState("");
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState<string | null>(null);

  // Re-sync when the server-side list changes (e.g. after a successful save
  // or when the parent switches modes and passes a different watchlist).
  useEffect(() => {
    setItems(initial);
    setDraft("");
    setError(null);
  }, [initial]);

  const dirty = useMemo(() => {
    if (items.length !== initial.length) return true;
    for (let i = 0; i < items.length; i++) {
      if (items[i] !== initial[i]) return true;
    }
    return false;
  }, [items, initial]);

  const normalize = (raw: string) => raw.trim().toUpperCase();
  const canAdd = (s: string) => s.length >= 3 && !items.includes(s);

  const add = () => {
    setError(null);
    const s = normalize(draft);
    if (!s) return;
    if (s.length < 3) {
      setError("Symbol must be at least 3 characters.");
      return;
    }
    if (items.includes(s)) {
      setError(`${s} is already in the list.`);
      return;
    }
    setItems([...items, s]);
    setDraft("");
  };

  const addSuggestion = (s: string) => {
    setError(null);
    if (items.includes(s)) return;
    setItems([...items, s]);
  };

  const remove = (s: string) => {
    setError(null);
    setItems(items.filter((x) => x !== s));
  };

  const save = async () => {
    setSaving(true);
    setError(null);
    try {
      await onSave(items);
    } catch (e) {
      setError(String(e));
    } finally {
      setSaving(false);
    }
  };

  const cancel = () => {
    setItems(initial);
    setDraft("");
    setError(null);
  };

  const draftNorm = normalize(draft);
  const draftInvalid = draft.length > 0 && !canAdd(draftNorm);

  return (
    <div className="space-y-3">
      {/* Current items as pills */}
      <div className="flex flex-wrap gap-1.5 min-h-[2rem]">
        {items.length === 0 ? (
          <span className="text-muted text-xs italic">
            No coins yet — add one below.
          </span>
        ) : (
          items.map((s) => (
            <span
              key={s}
              className="inline-flex items-center gap-1.5 px-2 py-1 rounded bg-bg-soft border border-border text-xs font-mono"
            >
              {s}
              <button
                onClick={() => remove(s)}
                disabled={disabled || saving}
                title={`Remove ${s}`}
                className="text-muted hover:text-short leading-none disabled:opacity-50"
                aria-label={`Remove ${s}`}
              >
                ×
              </button>
            </span>
          ))
        )}
      </div>

      {/* Suggestions (optional) */}
      {suggestions && suggestions.length > 0 && (
        <div className="text-xs text-muted">
          <span className="mr-2">Suggestions:</span>
          {suggestions
            .filter((s) => !items.includes(s))
            .slice(0, 8)
            .map((s) => (
              <button
                key={s}
                onClick={() => addSuggestion(s)}
                disabled={disabled || saving}
                className="mr-1 mb-1 inline-block px-1.5 py-0.5 rounded bg-bg-soft/70 border border-border hover:bg-bg-soft text-[11px] font-mono disabled:opacity-50"
              >
                + {s}
              </button>
            ))}
        </div>
      )}

      {/* Add input */}
      <div className="flex gap-2">
        <input
          type="text"
          value={draft}
          onChange={(e) => {
            setError(null);
            setDraft(e.target.value.toUpperCase());
          }}
          onKeyDown={(e) => {
            if (e.key === "Enter") {
              e.preventDefault();
              add();
            }
          }}
          placeholder={placeholder}
          disabled={disabled || saving}
          className={clsx(
            "flex-1 bg-bg-soft border rounded px-3 py-2 text-sm font-mono",
            draftInvalid ? "border-short/50" : "border-border",
          )}
        />
        <button
          onClick={add}
          disabled={disabled || saving || !draft.trim() || draftInvalid}
          className="px-4 py-2 bg-bg-soft border border-border hover:bg-border rounded text-sm disabled:opacity-40"
        >
          Add
        </button>
      </div>

      {/* Inline error */}
      {error && (
        <div className="text-xs text-short">{error}</div>
      )}

      {/* Save / Cancel — visible only when there are unsaved changes */}
      {dirty && (
        <div className="flex items-center gap-2 pt-1 border-t border-border/50">
          <span className="text-xs text-yellow-300">Unsaved changes</span>
          <div className="ml-auto flex gap-2">
            <button
              onClick={cancel}
              disabled={saving}
              className="px-4 py-2 bg-bg-soft border border-border rounded text-sm disabled:opacity-50"
            >
              Cancel
            </button>
            <button
              onClick={save}
              disabled={saving}
              className="px-4 py-2 bg-long/20 hover:bg-long/30 text-long border border-long/40 rounded text-sm disabled:opacity-50"
            >
              {saving ? "Saving…" : "Save changes"}
            </button>
          </div>
        </div>
      )}
    </div>
  );
}
