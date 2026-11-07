import type { PersonalModelConnection } from "../../api/contracts";

import { containsModel } from "./usePersonalChatModels";

type Props = {
  connections: readonly PersonalModelConnection[];
  value: string;
  label: string;
  placeholder: string;
  onChange: (id: string) => void;
  disabled?: boolean;
  busy?: boolean;
};

export function PersonalModelSelect({ connections, value, label, placeholder, onChange, disabled, busy }: Props) {
  const isKnown = containsModel(connections, value);
  return (
    <select aria-label={label} aria-busy={busy || undefined} disabled={disabled || busy}
      value={isKnown ? value : ""} onChange={(event) => onChange(event.currentTarget.value)}>
      {!isKnown && <option value="" disabled hidden>{placeholder}</option>}
      {connections.map((connection) => (
        <optgroup key={connection.connectionId} label={connection.name}>
          {connection.models.map((model) => (
            <option key={model.id} value={model.id}>{model.modelId}</option>
          ))}
        </optgroup>
      ))}
    </select>
  );
}
