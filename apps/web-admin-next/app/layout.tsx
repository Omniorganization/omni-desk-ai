import type { ReactNode } from 'react';
import './style.css';

export const metadata = { title: 'Omni Web Admin', description: 'Enterprise management console for OmniDesk' };

export default function RootLayout({ children }: { children: ReactNode }) {
  return <html lang="zh-CN"><body>{children}</body></html>;
}
