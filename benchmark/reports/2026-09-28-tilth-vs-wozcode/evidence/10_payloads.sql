SELECT arm,rep,tool_name,tool_use_id,cwd,input,text FROM responses WHERE tool_name LIKE 'mcp__%' ORDER BY arm,rep,timestamp
