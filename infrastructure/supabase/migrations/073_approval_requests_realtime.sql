-- Enable Supabase Realtime for approval_requests
-- Required for real-time authority queue push notifications (homepage, conference lobby, bell)
-- Safe DDL: approval_requests is a low-write-volume table

ALTER PUBLICATION supabase_realtime ADD TABLE public.approval_requests;
ALTER TABLE public.approval_requests REPLICA IDENTITY FULL;
