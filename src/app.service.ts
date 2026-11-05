import { Injectable, ServiceUnavailableException } from '@nestjs/common';
import { Client } from 'pg';

@Injectable()
export class AppService {
  getHello(): string {
    return 'Hello World!';
  }

  async checkDatabase(): Promise<void> {
    const connectionString = process.env.DATABASE_URL;
    if (!connectionString) {
      throw new ServiceUnavailableException('Database is unavailable');
    }

    const client = new Client({
      connectionString,
      connectionTimeoutMillis: 5000,
      query_timeout: 5000,
    });
    try {
      await client.connect();
      await client.query('SELECT 1');
    } catch {
      throw new ServiceUnavailableException('Database is unavailable');
    } finally {
      await client.end();
    }
  }
}
