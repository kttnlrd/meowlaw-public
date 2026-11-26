import React from 'react'
import ReactMarkdown from 'react-markdown'
import remarkGfm from 'remark-gfm'
import rehypeRaw from 'rehype-raw'
import { createMarkdownComponents } from './MarkdownRenderers'

type TreatyContentProps = {
  country: string
  articleData: any
  isMobile: boolean
  onNavigate: (act: string, section: string, anchor?: string) => void
  onNavigateRuling: (citation: string) => void
}

export default function TreatyContent({
  country,
  articleData,
  isMobile,
  onNavigate,
  onNavigateRuling,
}: TreatyContentProps) {
  const articleId = articleData?.article ?? ''
  const articleTitle = articleData?.title || ''
  const body = (articleData?.content || '').replace(/^---\n[\s\S]*?\n---\n?/, '').replace(/^#\s+[^\n]+\n?/, '')
  const components = createMarkdownComponents(isMobile, country, onNavigate, onNavigateRuling)

  // "Article 5 — Permanent Establishment" -> mono "Article 5" + display title
  const titleMatch = articleTitle.match(/^(Article\s+\S+)\s*[—–-]\s*(.+)$/)

  return (
    <div className={`lk-reader${isMobile ? ' lk-reader--mobile' : ''}`}>
      <nav aria-label="Breadcrumb" className="lk-reader-crumb">
        <span className="lk-badge lk-badge--treaty">Treaty</span>
        <span>{country}</span>
        <span className="lk-reader-crumb__sep" aria-hidden="true">/</span>
        <span>Article {articleId}</span>
      </nav>

      {articleTitle && (
        <h1 className="lk-reader-title">
          {titleMatch ? (
            <>
              <span className="lk-reader-title__num">{titleMatch[1]}</span>
              {titleMatch[2]}
            </>
          ) : articleTitle}
        </h1>
      )}

      <div className="lk-reader-body">
        <ReactMarkdown remarkPlugins={[remarkGfm]} rehypePlugins={[rehypeRaw]} components={components}>
          {body}
        </ReactMarkdown>
      </div>
    </div>
  )
}
