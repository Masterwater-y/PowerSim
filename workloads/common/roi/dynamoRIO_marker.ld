SECTIONS
{
  .fastsim_dynamoRIO_marker_text ALIGN(16) :
  {
    KEEP(*(.fastsim_dynamoRIO_marker_text))
  }
}
INSERT AFTER .text;
