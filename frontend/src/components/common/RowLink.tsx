// Thin wrapper around react-router's Link so existing onClick-div navigation
// rows can be converted to real <a href> elements (right-click / middle-click /
// ctrl-click "open in new tab" all work natively) with minimal call-site changes.
import React from 'react'
import { Link, LinkProps } from 'react-router-dom'

export type RowLinkProps = LinkProps & React.RefAttributes<HTMLAnchorElement>

const RowLink = React.forwardRef<HTMLAnchorElement, RowLinkProps>(function RowLink(props, ref) {
  const { style, ...rest } = props
  return (
    <Link
      ref={ref}
      {...rest}
      style={{ textDecoration: 'none', color: 'inherit', ...style }}
    />
  )
})

export default RowLink
