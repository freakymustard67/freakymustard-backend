// P2P egress probe: Sintel (well-seeded, webtorrent-friendly). Not part of smoke suite.
import WebTorrent from 'webtorrent'
const client = new WebTorrent()
const torrentId = {
  infoHash: '08ada5a7a6183aae1e09d831df6748d566095a10',
  announce: ['wss://tracker.btorrent.xyz','wss://tracker.fastcast.nz','wss://tracker.openwebtorrent.com',
             'udp://explodie.org:6969','udp://tracker.opentrackr.org:1337','udp://tracker.torrent.eu.org:451']
}
const t0 = Date.now()
const bail = setTimeout(() => { console.log('TIMEOUT 60s — no metadata. P2P likely blocked.'); process.exit(2) }, 60000)
client.on('error', e => console.log('client err:', e.message))
client.add(torrentId, (torrent) => {
  console.log('METADATA OK in', ((Date.now()-t0)/1000).toFixed(1)+'s:', torrent.name, `(${torrent.files.length} files)`)
  const f = torrent.files.find(f => /\.(mp4|mkv)$/i.test(f.name)) || torrent.files[0]
  f.select()
  let reported = false
  torrent.on('download', () => { if (!reported) { reported = true; console.log('DATA FLOWING ✓') } })
  setTimeout(() => {
    console.log(`FINAL downloaded=${torrent.downloaded} speed=${torrent.downloadSpeed} peers=${torrent.numPeers}`)
    process.exit(torrent.downloaded > 10000 ? 0 : 3)
  }, 20000)
})
