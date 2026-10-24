import { lazy, Suspense } from 'react';
import { BrowserRouter, Navigate, Route, Routes } from 'react-router-dom';
import Shell from './Shell';
const OverviewPage = lazy(() => import('./Overview').then(m => ({ default: m.OverviewPage })));
const IncidentRoom = lazy(() => import('./IncidentRoom').then(m => ({ default: m.IncidentRoom })));
import { Incidents } from './Incidents';
import { Ledger } from './Ledger';
import { Settings } from './Settings';
import './styles.css';
export default function App() {
  return <BrowserRouter><Suspense fallback={<div className="empty" role="status">화면을 불러오는 중입니다.</div>}><Routes><Route element={<Shell />}>
    <Route index element={<OverviewPage />} />
    <Route path="incidents" element={<Incidents />} />
    <Route path="incidents/:id" element={<IncidentRoom />} />
    <Route path="map" element={<Navigate to="/" replace />} />
    <Route path="ledger" element={<Ledger />} />
    <Route path="settings" element={<Settings />} />
    <Route path="*" element={<Navigate to="/" replace />} />
  </Route></Routes></Suspense></BrowserRouter>;
}
