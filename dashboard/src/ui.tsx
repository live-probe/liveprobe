import type { ReactNode } from 'react';
import { incidentStatusKo, incidentStatusTone, levelKo, levelTone } from './labels';
export const StatusBadge = ({ value }: { value: string }) => <span className={`badge ${incidentStatusTone(value)}`}>{incidentStatusKo(value)}</span>;
export const LevelBadge = ({ value }: { value: string | null | undefined }) => <span className={`badge ${levelTone(value)}`}>{levelKo(value)}</span>;
export const ErrorBox = ({ children }: { children?: ReactNode }) => children ? <div className="error" role="alert">{children}</div> : null;
export const Spinner = () => <span className="spinner" aria-hidden="true" />;
