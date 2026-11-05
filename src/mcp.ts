import { McpServer } from '@modelcontextprotocol/sdk/server/mcp.js';
import { StdioServerTransport } from '@modelcontextprotocol/sdk/server/stdio.js';

const server = new McpServer({ name: 'chungdo', version: '0.0.1' });

server.registerTool(
  'ping',
  { description: 'Check that the Chungdo MCP server responds.' },
  () => ({ content: [{ type: 'text', text: 'pong' }] }),
);

await server.connect(new StdioServerTransport());
