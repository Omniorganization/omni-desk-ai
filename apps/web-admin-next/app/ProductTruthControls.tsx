'use client';

import Link from 'next/link';
import { useEffect } from 'react';

import styles from './ProductTruthControls.module.css';

const UNIMPLEMENTED_SELECTORS = [
  '.setting-row',
];

export default function ProductTruthControls() {
  useEffect(() => {
    const disableKnownControls = () => {
      for (const selector of UNIMPLEMENTED_SELECTORS) {
        for (const element of document.querySelectorAll<HTMLButtonElement>(selector)) {
          if (element.dataset.implemented === 'true') continue;
          element.disabled = true;
          element.setAttribute('aria-disabled', 'true');
          element.title = element.title || '该功能尚未接入 Gateway';
          const action = element.querySelector('em');
          if (action) action.textContent = '未启用';
        }
      }
    };
    disableKnownControls();
    const observer = new MutationObserver(disableKnownControls);
    observer.observe(document.body, { childList: true, subtree: true });
    return () => observer.disconnect();
  }, []);

  return <Link className={styles.streamLink} href="/stream">流式工作区</Link>;
}
