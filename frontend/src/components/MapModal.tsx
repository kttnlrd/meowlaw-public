import React from 'react'
import MapView from './MapView'

interface Props {
  mapId: string
  onClose: () => void
  onOpenSection: (act: string, section: string) => void
}

export default function MapModal({ mapId, onClose, onOpenSection }: Props) {
  return (
    <div style={{ position: 'fixed', inset: 0, background: 'rgba(0,0,0,0.75)', zIndex: 1000, display: 'flex', flexDirection: 'column' }} onClick={onClose}>
      <div style={{ display: 'flex', flexDirection: 'column', height: '100%', maxWidth: 1400, margin: '0 auto', width: '100%', padding: 16 }} onClick={e => e.stopPropagation()}>
        <MapView mapId={mapId} onClose={onClose} onOpenSection={onOpenSection} height="100%" />
      </div>
    </div>
  )
}
