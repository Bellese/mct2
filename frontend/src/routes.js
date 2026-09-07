import MeasuresPage from './pages/MeasuresPage';
import JobsPage from './pages/JobsPage';
import PatientsPage from './pages/PatientsPage';
import ResultsPage from './pages/ResultsPage';
import SettingsPage from './pages/SettingsPage';
import ValidationPage from './pages/ValidationPage';
import { MeasuresIcon, JobsIcon, PatientsIcon, ResultsIcon, ValidateIcon } from './components/Icons';

// Single source of truth for every route in the app: the <Route> table, the
// sidebar nav + keyboard shortcuts, the header title, the search placeholder,
// and (validated by App.routes.test.js against public/serve.json) the
// production `serve` SPA rewrite list. Adding a page means adding one entry
// here — not touching five separate places (see #383, #384).
//
// Note: pages must not import this module — PatientsPage etc. are imported
// *by* routes.js, so the reverse would be circular.
//
// Field semantics:
// - `redirectTo`: this path renders a <Navigate> instead of a page component.
// - `nav`: present only for routes that appear in the sidebar and own a
//   keyboard shortcut (`kbd`). Omitted for `/settings` (rendered separately,
//   with its own styling and no shortcut) and for `/results/:jobId`.
// - `feature`: gates the *nav entry + keyboard shortcut* only. The <Route>
//   itself is always registered; the page component (e.g. ValidationPage) is
//   responsible for redirecting away if its own feature flag is off.
// - `title` / `searchPlaceholder`: looked up by basePath ('/' + first path
//   segment), which is why `/results/:jobId` omits them and resolves through
//   the `/results` entry instead.
export const ROUTES = [
  { path: '/', redirectTo: '/measures' },
  {
    path: '/measures',
    Component: MeasuresPage,
    title: 'Measures',
    searchPlaceholder: 'Search measures…',
    nav: { label: 'Measures', Icon: MeasuresIcon, kbd: 'M' },
  },
  {
    path: '/jobs',
    Component: JobsPage,
    title: 'Jobs',
    searchPlaceholder: 'Search jobs…',
    nav: { label: 'Jobs', Icon: JobsIcon, kbd: 'J' },
  },
  {
    path: '/patients',
    Component: PatientsPage,
    title: 'Patients',
    searchPlaceholder: 'Search patient groups…',
    nav: { label: 'Patients', Icon: PatientsIcon, kbd: 'P' },
  },
  // The module shipped as /groups while it was flag-gated (#322); keep the old
  // path working for anyone who bookmarked it.
  { path: '/groups', redirectTo: '/patients' },
  {
    path: '/results',
    Component: ResultsPage,
    title: 'Results',
    searchPlaceholder: 'Search patients…',
    nav: { label: 'Results', Icon: ResultsIcon, kbd: 'E' },
  },
  { path: '/results/:jobId', Component: ResultsPage },
  {
    path: '/validation',
    Component: ValidationPage,
    title: 'Validation',
    searchPlaceholder: 'Search validation runs…',
    nav: { label: 'Validation', Icon: ValidateIcon, kbd: 'V' },
    feature: 'validation',
  },
  {
    path: '/settings',
    Component: SettingsPage,
    title: 'Settings',
    searchPlaceholder: 'Search…',
  },
];
