import { useMemo, useSyncExternalStore } from 'react';
import type { AnchorHTMLAttributes } from 'react';

const locationChanged = 'app-location-changed';
let navigationVersion = 0;
window.addEventListener('popstate', () => { navigationVersion++; });
const snapshot = () => window.location.pathname + window.location.search + window.location.hash;

/** An async action may navigate only if no navigation happened while it ran. */
export function captureNavigation() {
  const version = navigationVersion;
  return () => version === navigationVersion;
}
function subscribe(listener: () => void) {
  window.addEventListener('popstate', listener);
  window.addEventListener(locationChanged, listener);
  return () => {
    window.removeEventListener('popstate', listener);
    window.removeEventListener(locationChanged, listener);
  };
}

export function navigate(to: string, { replace = false }: { replace?: boolean } = {}) {
  const url = new URL(to, window.location.href);
  if (url.origin !== window.location.origin) return;
  const next = url.pathname + url.search + url.hash;
  if (next === snapshot()) return;
  navigationVersion++;
  window.history[replace ? 'replaceState' : 'pushState'](null, '', next);
  window.dispatchEvent(new Event(locationChanged));
}

export function useRoute() {
  const location = useSyncExternalStore(subscribe, snapshot);
  const url = useMemo(() => new URL(location, window.location.origin), [location]);
  return { pathname: url.pathname, searchParams: url.searchParams };
}

/** Preserve native links, including copy-link, new tabs and modified clicks. */
export function Link({ href, onClick, ...props }: AnchorHTMLAttributes<HTMLAnchorElement> & { href: string }) {
  return <a {...props} href={href} onClick={event => {
    onClick?.(event);
    if (event.defaultPrevented || event.button !== 0 || event.metaKey || event.ctrlKey || event.shiftKey || event.altKey
      || (props.target && props.target !== '_self') || props.download != null) return;
    const url = new URL(href, window.location.href);
    if (url.origin !== window.location.origin) return;
    event.preventDefault();
    navigate(href);
  }} />;
}

export type Page = 'training' | 'stats' | 'library' | 'analysis' | 'coach' | 'login' | 'not-found';
export function pageForPath(pathname: string): Page {
  if (pathname === '/' || pathname === '/training') return 'training';
  if (pathname === '/progress') return 'stats';
  const library = /^\/games(?:\/([1-9]\d*))?$/.exec(pathname);
  if (library && (!library[1] || Number.isSafeInteger(Number(library[1])))) return 'library';
  const analysis = /^\/analysis(?:\/([1-9]\d*)(?:\/review)?)?$/.exec(pathname);
  if (analysis && (!analysis[1] || Number.isSafeInteger(Number(analysis[1])))) return 'analysis';
  const coach = /^\/coach(?:\/(?:skills|progress|games|settings|practice\/([1-9]\d*)))?$/.exec(pathname);
  if (coach && (!coach[1] || Number.isSafeInteger(Number(coach[1])))) return 'coach';
  if (pathname === '/login') return 'login';
  return 'not-found';
}

export function loginDestination(value: string | null) {
  if (!value?.startsWith('/') || value.startsWith('//') || value.includes('\\')) return '/';
  const url = new URL(value, window.location.origin);
  if (url.origin !== window.location.origin || pageForPath(url.pathname) === 'login') return '/';
  return url.pathname + url.search + url.hash;
}
