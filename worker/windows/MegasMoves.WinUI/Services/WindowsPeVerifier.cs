using System.Buffers.Binary;

namespace MegasMoves.Services;

public static class WindowsPeVerifier
{
    private const uint PeSignature = 0x00004550;
    private const ushort MachineAmd64 = 0x8664;
    private const ushort OptionalMagicPe32Plus = 0x020B;
    private const ushort SubsystemWindowsGui = 2;
    private const ushort ExecutableImage = 0x0002;
    private const int MaxPeHeaderOffset = 16 * 1024 * 1024;

    public static void ValidateMegasMovesAmd64Gui(string path)
    {
        using var stream = new FileStream(
            path,
            FileMode.Open,
            FileAccess.Read,
            FileShare.Read,
            4096,
            FileOptions.RandomAccess);
        if (stream.Length < 512)
        {
            throw new InvalidDataException("Staged MegasMoves.exe is too small to be a valid Windows PE.");
        }

        Span<byte> dos = stackalloc byte[64];
        ReadExactly(stream, dos);
        if (dos[0] != (byte)'M' || dos[1] != (byte)'Z')
        {
            throw new InvalidDataException("Staged update is not a Windows MZ executable.");
        }

        var peOffset = BinaryPrimitives.ReadInt32LittleEndian(dos[0x3C..0x40]);
        if (peOffset < 64 || peOffset > MaxPeHeaderOffset || peOffset + 24 + 70 > stream.Length)
        {
            throw new InvalidDataException("Staged update has an invalid PE header offset.");
        }

        stream.Position = peOffset;
        Span<byte> header = stackalloc byte[24];
        ReadExactly(stream, header);
        if (header[0] != (byte)'P' || header[1] != (byte)'E' || header[2] != 0 || header[3] != 0)
        {
            throw new InvalidDataException("Staged update has an invalid PE signature.");
        }

        var machine = BinaryPrimitives.ReadUInt16LittleEndian(header[4..6]);
        var sections = BinaryPrimitives.ReadUInt16LittleEndian(header[6..8]);
        var optionalHeaderSize = BinaryPrimitives.ReadUInt16LittleEndian(header[20..22]);
        var characteristics = BinaryPrimitives.ReadUInt16LittleEndian(header[22..24]);
        if (machine != MachineAmd64 || sections == 0 || (characteristics & ExecutableImage) == 0)
        {
            throw new InvalidDataException("Staged update is not an executable AMD64 Windows image.");
        }
        if (optionalHeaderSize < 70 || peOffset + 24L + optionalHeaderSize > stream.Length)
        {
            throw new InvalidDataException("Staged update has an invalid optional header.");
        }

        Span<byte> optional = stackalloc byte[70];
        ReadExactly(stream, optional);
        if (BinaryPrimitives.ReadUInt16LittleEndian(optional[0..2]) != OptionalMagicPe32Plus)
        {
            throw new InvalidDataException("Staged update is not a PE32+ image.");
        }
        if (BinaryPrimitives.ReadUInt16LittleEndian(optional[68..70]) != SubsystemWindowsGui)
        {
            throw new InvalidDataException("Staged update is not a Windows GUI executable.");
        }
    }

    private static void ReadExactly(Stream stream, Span<byte> buffer)
    {
        var offset = 0;
        while (offset < buffer.Length)
        {
            var read = stream.Read(buffer[offset..]);
            if (read <= 0)
            {
                throw new EndOfStreamException("Unexpected end of staged PE file.");
            }
            offset += read;
        }
    }
}
