#!/usr/bin/python

import sys, os, stat, shutil, shlex, hashlib, collections
join = os.path.join

TIER_CONFIG = join(os.getenv('HOME'), '.config', 'tier.conf')

NONE = 0
FILE = 1
LINK = 2
OTHER = 3

def UnpackBits(bits, tiers):
  out = ''
  for t in range(tiers):
    tp = (bits & (3 << (t * 2))) >> (t * 2)
    if tp == NONE:
      out += 'N'
    elif tp == FILE:
      out += 'F'
    elif tp == LINK:
      out += 'L'
    elif tp == OTHER:
      out += 'O'
  return out

def TimeAndSize(fn):
  st = os.lstat(fn)
  return int(st.st_mtime), st.st_size

def GetType(fn):
  try:
    mode = os.lstat(fn).st_mode
    if stat.S_ISREG(mode):
      return FILE
    elif stat.S_ISLNK(mode):
      return LINK
    else:
      return OTHER
  except OSError:
    return NONE

def AllFilesInTree(t):
  assert t.endswith('/')
  for path, _, fns in os.walk(t):
    assert path.startswith(t)
    path = path[len(t):]
    for fn in fns:
      fn = join(path, fn)
      yield fn

def Fileprint(fn):
  CHUNK = 1024
  f = open(fn)
  s = hashlib.sha1()
  s.update(f.read(CHUNK))
  f.seek(0, 2)
  l = f.tell()
  f.seek(l / 2 / CHUNK * CHUNK)
  s.update(f.read(CHUNK))
  f.seek(max(0, (l - CHUNK) / CHUNK * CHUNK))
  s.update(f.read(CHUNK))
  return s.digest()

def FmtInts(ints):
  return ','.join(map(str, ints))


class TierManager(object):
  def __init__(self, config):
    # Paths in self.tiers end in /
    self.tiers = []
    self.LoadConfig(config)

  def LoadConfig(self, config):
    for line in config.splitlines():
      line = shlex.split(line, comments=True)
      if line and line[0] == 'tier':
        tier = line[1].rstrip('/') + '/'
        assert tier.startswith('/')
        self.tiers.append(tier)

  def WhichTier(self, path):
    """Returns: tuple of:
      tier index (-1 if not in any tier)
      relative path inside tier (original path if not in any tier)
    """
    path = os.path.abspath(path)
    for i, t in enumerate(self.tiers):
      if path.startswith(t):
        return i, path[len(t):]
    return -1, path

  def InTier(self, t, relpath):
    return self.tiers[t] + relpath

  def FullMap(self):
    """Returns:
      map from relative filename to packed types
    """
    full = collections.defaultdict(int)
    for i, t in enumerate(self.tiers):
      for f in AllFilesInTree(t):
        tp = GetType(join(t, f))
        full[f] |= (tp << (i * 2))
    return full

  def CheckConsistency(self):
    tcount = len(self.tiers)
    full = self.FullMap()
    full = full.items()
    full.sort()
    for relpath, bits in full:
      tps = UnpackBits(bits, tcount)
      fixes = []
      # Find first file (most accessible copy). Above this should be links to
      # this, below this should be identical copies of this.
      ff = tps.find('F')
      if ff < 0:
        fixes.append('No files exist!')
      else:
        # These should be links to the file at ff.
        for i in xrange(0, ff):
          if tps[i] != 'L':
            fixes.append('Symlink from %d to %d' % (i, ff))
          else:
            target = os.readlink(self.InTier(i, relpath))
            if target != self.InTier(ff, relpath):
              fixes.append('Change symlink target of %d' % i)
        # {(mtime, size): [index]}
        data_candidates = collections.defaultdict(list)
        # Check the file at ff.
        ts0 = TimeAndSize(self.InTier(ff, relpath))
        data_candidates[ts0].append(ff)
        # These should be files.
        for i in xrange(ff+1, tcount):
          if tps[i] != 'F':
            fixes.append('Copy from %d to %d' % (ff, i))
          else:
            ts1 = TimeAndSize(self.InTier(i, relpath))
            data_candidates[ts1].append(i)
        if len(data_candidates) > 1:
          data = data_candidates.items()
          data.sort(reverse=True)  # highest mtime first
          froms = data[0][1]
          tos = []
          for _, indexes in data[1:]:
            tos.extend(indexes)
          fixes.append('Copy data from %s to %s' % (
            FmtInts(froms), FmtInts(tos)))

      if fixes:
        print '%s: %s' % (repr(relpath)[1:-1], tps)
        for f in fixes:
          print '  ', f


def main(argv):
  config = open(TIER_CONFIG).read()
  tier = TierManager(config)
  tier.CheckConsistency()

if __name__ == '__main__':
  sys.exit(main(sys.argv))
