import { Logger } from '@nestjs/common';
import { NestFactory } from '@nestjs/core';
import { Client } from 'pg';
import { AppModule } from './app.module.js';

async function checkDatabase(connectionString: string) {
  const logger = new Logger('PostgreSQL');
  let client: Client | undefined;

  try {
    client = new Client({
      connectionString,
      connectionTimeoutMillis: 5000,
      query_timeout: 5000,
    });
    await client.connect();
    await client.query('SELECT 1');
    logger.log('Database connection verified');
  } catch {
    logger.error('Database connection check failed');
  } finally {
    await client?.end();
  }
}

async function bootstrap() {
  const app = await NestFactory.create(AppModule);
  await app.listen(process.env.PORT ?? 3000);

  if (process.env.DATABASE_URL) {
    await checkDatabase(process.env.DATABASE_URL);
  }
}
await bootstrap();
