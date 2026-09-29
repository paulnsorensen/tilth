SELECT arm,tool_name,count(*) calls,count(*) FILTER(WHERE is_error='true') explicit_errors FROM calls GROUP BY arm,tool_name ORDER BY arm,calls DESC;
