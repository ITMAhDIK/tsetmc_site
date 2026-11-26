-- Analytics route sequence reporting.
-- Existing opted-in page_view rows are the source of truth; route sequences are
-- derived at query time so they cannot drift from the event log.
CREATE INDEX IF NOT EXISTS idx_analytics_path_sequence_session
 ON analytics_events(user_id,session_id,occurred_at,id)
 INCLUDE(page_path)
 WHERE user_id IS NOT NULL
   AND session_id IS NOT NULL
   AND event_name='page_view';

CREATE INDEX IF NOT EXISTS idx_analytics_path_sequence_time
 ON analytics_events(occurred_at)
 INCLUDE(user_id,session_id,page_path,id)
 WHERE user_id IS NOT NULL
   AND session_id IS NOT NULL
   AND event_name='page_view';
