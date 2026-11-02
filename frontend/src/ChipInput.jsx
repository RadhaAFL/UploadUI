import { useState } from 'react'

// Tag input: type a value and press Enter or comma to add it as a chip;
// Backspace on an empty box removes the last chip.
export default function ChipInput({ value, onChange, placeholder, disabled }) {
  const [draft, setDraft] = useState('')

  const commit = () => {
    const parts = draft.split(',').map(s => s.trim()).filter(Boolean)
    if (!parts.length) return
    onChange([...new Set([...value, ...parts])])
    setDraft('')
  }

  const onKeyDown = e => {
    if (e.key === 'Enter' || e.key === ',') { e.preventDefault(); commit() }
    else if (e.key === 'Backspace' && !draft && value.length) onChange(value.slice(0, -1))
  }

  return (
    <div className={`chipinput ${disabled ? 'chipinput-off' : ''}`}>
      {value.map(v => (
        <span key={v} className="chipinput-chip">
          {v}
          <button type="button" aria-label={`Remove ${v}`} disabled={disabled}
                  onClick={() => onChange(value.filter(x => x !== v))}>✕</button>
        </span>
      ))}
      <input value={draft} disabled={disabled} placeholder={value.length ? '' : placeholder}
             onChange={e => setDraft(e.target.value)} onKeyDown={onKeyDown} onBlur={commit} />
    </div>
  )
}
