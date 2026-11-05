import { Logger } from '@nestjs/common';
import { NestFactory } from '@nestjs/core';
import { AppModule } from './app.module.js';
import { AppService } from './app.service.js';

async function bootstrap() {
  const app = await NestFactory.create(AppModule);
  await app.listen(process.env.PORT ?? 3000);

  if (process.env.DATABASE_URL) {
    const logger = new Logger('PostgreSQL');
    try {
      await app.get(AppService).checkDatabase();
      logger.log('Database connection verified');
    } catch {
      logger.error('Database connection check failed');
    }
  }
}
await bootstrap();
