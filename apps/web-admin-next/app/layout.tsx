import type { ReactNode } from 'react';
import { connection } from 'next/server';

import ProductTruthControls from './ProductTruthControls';
import './style.css';

export const metadata = {
  title: 'Omni Web Admin',
  description: 'Enterprise management console for OmniDesk',
};

export default async function RootLayout({ children }: { children: ReactNode }) {
  await connection();
  return <html lang="zh-CN"><body>{children}<ProductTruthControls /></body></html>;
}
