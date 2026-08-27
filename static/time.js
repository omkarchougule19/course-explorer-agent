// Shared by index.html and freshness.html. scraped_at is stored as SQLite's
// CURRENT_TIMESTAMP, e.g. "2026-08-25 14:03:11" - UTC, but with no timezone
// marker, so browsers would otherwise parse it as local time. Force UTC.
function parseUtc(sqliteTimestamp) {
  return new Date(sqliteTimestamp.replace(' ', 'T') + 'Z');
}

function formatRelativeTime(sqliteTimestamp) {
  if (!sqliteTimestamp) return '—';
  const date = parseUtc(sqliteTimestamp);
  const seconds = Math.round((Date.now() - date.getTime()) / 1000);
  if (seconds < 60) return 'just now';
  const minutes = Math.round(seconds / 60);
  if (minutes < 60) return `${minutes}m ago`;
  const hours = Math.round(minutes / 60);
  if (hours < 24) return `${hours}h ago`;
  const days = Math.round(hours / 24);
  if (days < 30) return `${days}d ago`;
  const months = Math.round(days / 30);
  if (months < 12) return `${months}mo ago`;
  return `${Math.round(months / 12)}y ago`;
}
